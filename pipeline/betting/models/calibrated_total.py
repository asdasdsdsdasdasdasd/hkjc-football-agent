"""Leakage-safe market blending for match-total O/U probabilities.

p_final = w * p_raw + (1 - w) * p_market

The blend weight w is estimated only from matches strictly before the target date,
using a time-split base/calibration window inside the training pool.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from pipeline.betting.devig import implied_over_under
from pipeline.betting.load import parse_match_date, parse_score_total
from pipeline.betting.models.score_poisson import (
    ScorelineState,
    expected_total_over_under_roi,
    fit_scoreline_model,
    predict_total_side_probability,
)
from pipeline.betting.settlement import settle_over_under
from pipeline.betting.types import BetOutcome

MODEL_VERSION = "v5"
DEFAULT_WEIGHT_GRID = tuple(round(x * 0.05, 2) for x in range(0, 21))  # 0.00 .. 1.00
MIN_CALIBRATION_SAMPLES = 40
MARKET_FALLBACK_WEIGHT = 0.25  # when sparse, lean toward market (1-w)


@dataclass(frozen=True)
class BlendState:
    """Calibrator for one period (full_time / half_time)."""

    weight_raw: float
    n: int
    period: str
    log_loss: float
    source: str = "fit"  # fit | fallback_market | identity_raw


def clip01(value: float) -> float:
    return min(1.0 - 1e-6, max(1e-6, value))


def blend_probability(p_raw: float, p_market: float | None, weight_raw: float) -> float:
    if p_market is None:
        return clip01(p_raw)
    w = min(1.0, max(0.0, weight_raw))
    return clip01(w * p_raw + (1.0 - w) * float(p_market))


def blend_roi(roi_raw: float, roi_market: float | None, weight_raw: float) -> float:
    if roi_market is None:
        return roi_raw
    w = min(1.0, max(0.0, weight_raw))
    return w * roi_raw + (1.0 - w) * float(roi_market)


def log_loss(y: float, p: float) -> float:
    p = clip01(p)
    return -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))


def _score_key(period: str) -> str:
    return "half_time" if period == "half_time" else "full_time"


def _odds_section(period: str) -> str:
    return "半場入球大細" if period == "half_time" else "入球大細"


def _binary_label(total: int, line: str, side: str) -> float | None:
    outcome = settle_over_under(total, line, side)
    if outcome == BetOutcome.WIN:
        return 1.0
    if outcome == BetOutcome.LOSE:
        return 0.0
    return None


def _sorted_by_date(matches: list[dict[str, Any]]) -> list[tuple[date, dict[str, Any]]]:
    rows: list[tuple[date, dict[str, Any]]] = []
    for match in matches:
        try:
            rows.append((parse_match_date(match["date"]), match))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda item: (item[0], str(item[1].get("match_id") or "")))
    return rows


def _collect_ou_rows(match: dict[str, Any], period: str) -> list[tuple[str, str, float, float, float]]:
    """Return (line, side, over_odds, under_odds, y) for settleable lines."""
    score_key = _score_key(period)
    total = parse_score_total((match.get("scores") or {}).get(score_key))
    if total is None:
        return []
    section = _odds_section(period)
    entries = (match.get("odds_closing") or {}).get(section) or []
    out: list[tuple[str, str, float, float, float]] = []
    for entry in entries:
        line = entry.get("line")
        over_odds = entry.get("over_odds")
        under_odds = entry.get("under_odds")
        if not line or over_odds is None or under_odds is None:
            continue
        for side in ("over", "under"):
            y = _binary_label(int(total), str(line), side)
            if y is None:
                continue
            out.append((str(line), side, float(over_odds), float(under_odds), y))
    return out


def fit_blend_weight(
    samples: Iterable[tuple[float, float, float]],
    *,
    grid: tuple[float, ...] = DEFAULT_WEIGHT_GRID,
    period: str = "full_time",
) -> BlendState:
    """Choose w minimizing average log loss on (p_raw, p_market, y) samples."""
    rows = [(float(p_raw), float(p_mkt), float(y)) for p_raw, p_mkt, y in samples]
    n = len(rows)
    if n < MIN_CALIBRATION_SAMPLES:
        return BlendState(
            weight_raw=MARKET_FALLBACK_WEIGHT,
            n=n,
            period=period,
            log_loss=0.0,
            source="fallback_market",
        )

    best_w = MARKET_FALLBACK_WEIGHT
    best_loss = float("inf")
    for w in grid:
        total = 0.0
        for p_raw, p_mkt, y in rows:
            p = blend_probability(p_raw, p_mkt, w)
            total += log_loss(y, p)
        avg = total / n
        if avg < best_loss:
            best_loss = avg
            best_w = w
    return BlendState(weight_raw=best_w, n=n, period=period, log_loss=best_loss, source="fit")


def fit_period_blend(
    train_matches: list[dict[str, Any]],
    *,
    period: str,
    min_team_matches: int = 4,
    min_comp_matches: int = 20,
    calibration_fraction: float = 0.25,
) -> BlendState:
    """Fit scoreline on early train days; estimate blend weight on later days."""
    dated = _sorted_by_date(train_matches)
    if len(dated) < 30:
        return BlendState(
            weight_raw=MARKET_FALLBACK_WEIGHT,
            n=0,
            period=period,
            log_loss=0.0,
            source="fallback_market",
        )

    unique_days = sorted({d for d, _ in dated})
    split_idx = max(1, int(len(unique_days) * (1.0 - calibration_fraction)))
    if split_idx >= len(unique_days):
        split_idx = len(unique_days) - 1
    cutoff = unique_days[split_idx]
    base = [m for d, m in dated if d < cutoff]
    cal = [m for d, m in dated if d >= cutoff]
    if len(base) < 20 or not cal:
        return BlendState(
            weight_raw=MARKET_FALLBACK_WEIGHT,
            n=0,
            period=period,
            log_loss=0.0,
            source="fallback_market",
        )

    score_key = _score_key(period)
    state = fit_scoreline_model(
        base,
        score_key=score_key,
        min_team_matches=min_team_matches,
        min_comp_matches=min_comp_matches,
    )
    if state is None:
        return BlendState(
            weight_raw=MARKET_FALLBACK_WEIGHT,
            n=0,
            period=period,
            log_loss=0.0,
            source="fallback_market",
        )

    samples: list[tuple[float, float, float]] = []
    for match in cal:
        for line, side, over_odds, under_odds, y in _collect_ou_rows(match, period):
            p_market = implied_over_under(over_odds, under_odds, side)
            if p_market is None:
                continue
            p_raw = predict_total_side_probability(
                state,
                match,
                line_raw=line,
                side=side,
                score_key=score_key,
            )
            samples.append((p_raw, p_market, y))
    return fit_blend_weight(samples, period=period)


def fit_goal_model_state(
    train_matches: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any] | None:
    """Return ModelState.data payload for goal_ou adapters."""
    score_key = _score_key(period)
    scoreline = fit_scoreline_model(train_matches, score_key=score_key)
    if scoreline is None:
        return None
    blend = fit_period_blend(train_matches, period=period)
    return {
        "scoreline": scoreline,
        "blend": blend,
        "period": period,
        "score_key": score_key,
        "model_version": MODEL_VERSION,
    }


def predict_calibrated_probability(
    payload: dict[str, Any],
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    over_odds: float | None,
    under_odds: float | None,
) -> dict[str, float | str | int | None]:
    scoreline: ScorelineState = payload["scoreline"]
    blend: BlendState = payload["blend"]
    score_key = str(payload.get("score_key") or _score_key(str(payload.get("period") or "full_time")))
    p_raw = predict_total_side_probability(
        scoreline,
        match,
        line_raw=line_raw,
        side=side,
        score_key=score_key,
    )
    p_market = implied_over_under(over_odds, under_odds, side)
    p_final = blend_probability(p_raw, p_market, blend.weight_raw)
    return {
        "p_raw": round(p_raw, 6),
        "p_market": round(p_market, 6) if p_market is not None else None,
        "p_final": round(p_final, 6),
        "blend_weight": round(blend.weight_raw, 4),
        "calibration_n": int(blend.n),
        "calibration_source": blend.source,
        "model_version": MODEL_VERSION,
    }


def expected_calibrated_roi(
    payload: dict[str, Any],
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    decimal_odds: float,
    over_odds: float | None,
    under_odds: float | None,
) -> float:
    scoreline: ScorelineState = payload["scoreline"]
    blend: BlendState = payload["blend"]
    score_key = str(payload.get("score_key") or _score_key(str(payload.get("period") or "full_time")))
    roi_raw = expected_total_over_under_roi(
        scoreline,
        match,
        line_raw=line_raw,
        side=side,
        decimal_odds=decimal_odds,
        score_key=score_key,
    )
    p_market = implied_over_under(over_odds, under_odds, side)
    roi_market = (p_market * decimal_odds - 1.0) if p_market is not None else None
    return blend_roi(roi_raw, roi_market, blend.weight_raw)
