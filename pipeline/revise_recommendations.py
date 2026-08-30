#!/usr/bin/env python3
"""Revise BET recommendations using form, xG, weather, play-style, and model."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.analyze_recommendations import _fmt_pick, _revised_side_ev, style_note_from_form
from pipeline.betting.devig import devig_multi_way, implied_over_under
from pipeline.betting.load import load_matches, parse_match_date, split_teams
from pipeline.betting.markets import DEFAULT_MARKET_KEYS, get_adapters
from pipeline.betting.markets.team_ou import team_role_from_market
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.models.poisson_total import predict_match_mu, predict_side_probability, predict_team_mu, fit_poisson_total
from pipeline.betting.models.dixon_coles import (
    fit_dc_cached,
    match_ref_date,
    predict_mus,
    predict_side_probability_dc,
    predict_total_mu,
)

# Dixon-Coles engine validated out-of-sample 2026-07-24..08-28 (log-loss beats
# poisson_total on HT goals 0.6642 vs 0.6720 and FT corners 0.7050 vs 0.7160).
USE_DC_MODEL = True
from pipeline.betting.recent_form import match_form_summary
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.snapshots import latest_snapshot
from pipeline.betting.types import BetOpportunity

# BSD pre-match xG is full-match; scale for HT goal markets (~45% of FT goals).
HT_GOAL_XG_SHARE = 0.45

# v3.2 (2026-08-22): market-aware composite.  Legacy composite summed
# ev_base + form + xG + weather + style biases, which double-counts
# "obvious low/high-scoring match" evidence the bookmaker already priced
# into the odds.  Live 08-14..21: composite<=0.25 → +17.8% ROI; >0.25 →
# -35.6%.  New composite = disagreement with market, not agreement with side.
MARKET_AWARE_COMPOSITE = True

# The team-goal model has no historical closing-odds backtest yet. Keep its
# predictions visible for data collection, but do not expose it as a live bet.
TEAM_GOAL_MARKETS = frozenset({"goal_ou_home_ft", "goal_ou_away_ft", "goal_ou_home_ht", "goal_ou_away_ht"})
HIGH_ODDS_GOAL_OVER = 2.50
HIGH_ODDS_HT_GOAL_OVER = 2.20

# v4 live policy (2026-07-23): stop revise expansion and uncalibrated sides.
# Evidence window 07-11..22: NEW -16.9% ROI, over -23.9%, corners mostly unsettleable.
POLICY_VERSION = "v4"
LIVE_MIN_OLD_P = 0.80
LIVE_MIN_ODDS = 1.40  # below this, break-even >71%; chalk unders need near-perfect calibration
MARKET_EDGE_MIN = 0.05
MARKET_EDGE_MAX = 0.30  # larger gaps are Poisson hallucination vs market


def _is_team_goal_market(market: str) -> bool:
    return market in TEAM_GOAL_MARKETS


def _is_corner_market(market: str) -> bool:
    return "corner" in market


def _is_high_odds_goal_over(opp: BetOpportunity) -> bool:
    """Reject uncalibrated long-priced goal overs from live recommendations."""
    if "goal" not in opp.market or opp.side != "over":
        return False
    threshold = HIGH_ODDS_HT_GOAL_OVER if opp.market.endswith("_ht") else HIGH_ODDS_GOAL_OVER
    return opp.decimal_odds >= threshold


def _is_model_conflict(sc: ScoredLine) -> bool:
    return sc.model_side is not None and sc.model_side != sc.opp.side


def _passes_market_consistency(sc: ScoredLine) -> bool:
    """Require either positive market EV or a bounded model-vs-market edge."""
    if sc.market_ev > 0:
        return True
    if sc.market_p is None:
        return False
    edge = float(sc.old_p) - float(sc.market_p)
    return MARKET_EDGE_MIN <= edge <= MARKET_EDGE_MAX


def _is_orig_keep_or_flip(key: tuple[str, str, str], orig_keys: set[tuple[str, str, str]]) -> bool:
    """Live bets must come from the original top3 seed (KEEP) or a FLIP of that line."""
    if key in orig_keys:
        return True
    market, line, side = key
    return any(ok[0] == market and ok[1] == line and ok[2] != side for ok in orig_keys)


def _paper_reason(
    sc: ScoredLine,
    *,
    is_seeded: bool,
    match_seeded: bool = True,
    policy: str = POLICY_VERSION,
) -> str | None:
    """Return why a passing line is PAPER-only, else None for live-eligible."""
    market = sc.opp.market
    side = sc.opp.side
    if _is_team_goal_market(market):
        return "team-goal market: paper trade pending historical validation"
    # v3 live book: corners, overs, and NEW lines are allowed (cap happens later).
    if policy == "v3":
        if _is_high_odds_goal_over(sc.opp):
            return "high-odds goal-over: paper pending validation"
        return None
    # v3.1: corners and unders are live-eligible; overs stay paper. Stricter
    # market/odds/composite/weekend cuts are applied later in cap_live_v31.
    if policy == "v3.1":
        if _is_high_odds_goal_over(sc.opp):
            return "high-odds goal-over: paper pending validation"
        if side == "over":
            return "over side: paper (v3.1 live is unders only)"
        return None
    if _is_corner_market(market):
        return "corner market: paper pending settleable corner data"
    if side == "over":
        return "over side: paper pending forward validation (07-11..22 ROI -23.9%)"
    if not is_seeded:
        # Allow revise to upgrade to match-total goal under on a top3-seeded match.
        allowed_upgrade = match_seeded and market in {"goal_ou_ft", "goal_ou_ht"} and side == "under"
        if not allowed_upgrade:
            return "NEW line outside original top3: paper only (07-11..22 NEW ROI -16.9%)"
    if float(sc.old_p) < LIVE_MIN_OLD_P:
        return f"live requires under with old_p>={LIVE_MIN_OLD_P:.2f}"
    if float(sc.opp.decimal_odds) < LIVE_MIN_ODDS:
        return f"live requires odds>={LIVE_MIN_ODDS:.2f} (reject juice chalk)"
    if not _passes_market_consistency(sc):
        return "failed market consistency (need market_ev>0 or bounded model-market edge)"
    return None


def _line_float(line: str) -> float:
    m = re.search(r"[\d.]+", line or "")
    return float(m.group()) if m else 0.0


def _market_p(opp: BetOpportunity, group: list[BetOpportunity]) -> float | None:
    if opp.side in ("over", "under"):
        return implied_over_under(opp.over_odds, opp.under_odds, opp.side)
    odds_by_side = {item.side: item.decimal_odds for item in group}
    return devig_multi_way(odds_by_side).get(opp.side)


def _form_period_key(market: str) -> tuple[str, str] | None:
    if "corner" in market:
        return "corner_ht" if market.endswith("_ht") else "corner_ft", "corner"
    if "goal" in market:
        return "goal_ht" if market.endswith("_ht") else "goal_ft", "goal"
    return None


def _form_match_total_avg(form: dict[str, Any], market: str) -> float | None:
    parsed = _form_period_key(market)
    if parsed is None:
        return None
    key, _ = parsed
    team_role = team_role_from_market(market)
    if team_role is not None:
        avg = form.get(f"{key}_{team_role}_avg")
        if avg is not None:
            return float(avg)
        match_avg = form.get(f"{key}_{team_role}_match_total_avg")
        if match_avg is not None:
            return float(match_avg)
    h = form.get(f"{key}_home_match_total_avg")
    a = form.get(f"{key}_away_match_total_avg")
    if h is None and a is None:
        h = form.get(f"{key}_home_avg")
        a = form.get(f"{key}_away_avg")
    if h is None or a is None:
        if h is not None:
            return float(h)
        if a is not None:
            return float(a)
        return None
    return (float(h) + float(a)) / 2.0


def _form_sample_depth(form: dict[str, Any], market: str) -> int:
    parsed = _form_period_key(market)
    if parsed is None:
        return 99
    key, _ = parsed
    team_role = team_role_from_market(market)
    if team_role is not None:
        rows = form.get(f"{key}_{team_role}_last5") or []
        return len(rows)
    home = form.get(f"{key}_home_last5") or []
    away = form.get(f"{key}_away_last5") or []
    if not home and not away:
        return 0
    return min(len(home), len(away))


def _xg_expected_total(market: str, hx: float, ax: float) -> float:
    """Map BSD full-match xG to the period implied by the market."""
    team_role = team_role_from_market(market)
    if team_role == "home":
        total = hx
    elif team_role == "away":
        total = ax
    else:
        total = hx + ax
    if "goal" in market and market.endswith("_ht"):
        return total * HT_GOAL_XG_SHARE
    return total


def _form_bias(
    form: dict[str, Any],
    market: str,
    line: str,
    side: str,
    *,
    xg_effective: float | None = None,
) -> float:
    avg = _form_match_total_avg(form, market)
    if avg is None:
        return 0.0
    line_val = _line_float(line)
    delta = avg - line_val
    if side == "over":
        if delta >= 1.0:
            bias = 0.12
        elif delta >= 0.3:
            bias = 0.05
        elif delta <= -1.0:
            bias = -0.15
        elif delta <= -0.3:
            bias = -0.06
        else:
            bias = 0.0
    else:
        if delta <= -1.0:
            bias = 0.12
        elif delta <= -0.3:
            bias = 0.05
        elif delta >= 1.0:
            bias = -0.15
        elif delta >= 0.3:
            bias = -0.06
        else:
            bias = 0.0
    # BSD xG below the line beats noisy "recent goals avg" form narratives on overs.
    if (
        "goal" in market
        and side == "over"
        and xg_effective is not None
        and xg_effective < line_val
        and bias > 0
    ):
        return 0.0
    return bias


def _xg_bias(features: dict[str, float], market: str, line: str, side: str) -> tuple[float, str]:
    if "goal" not in market:
        return 0.0, ""
    hx = features.get("home_xg")
    ax = features.get("away_xg")
    if hx is None or ax is None:
        return 0.0, ""
    ft_total = hx + ax
    total = _xg_expected_total(market, hx, ax)
    line_val = _line_float(line)
    period = "HT" if market.endswith("_ht") else "FT"
    note = f"xG {period} exp {total:.2f} (BSD FT {ft_total:.2f}; H {hx:.2f}+A {ax:.2f})"
    margin = 0.25
    if side == "over":
        if total >= line_val + margin:
            return 0.10, note + " → fav over"
        if total < line_val:
            gap = line_val - total
            penalty = min(0.18, 0.08 + gap * 0.25)
            return -penalty, note + f" → below line ({total:.2f}<{line_val:.1f}) fav under"
        if total <= line_val - margin:
            return -0.12, note + " → fav under"
    else:
        if total <= line_val - margin:
            return 0.10, note + " → fav under"
        if total > line_val:
            gap = total - line_val
            penalty = min(0.18, 0.08 + gap * 0.25)
            return -penalty, note + f" → above line fav over"
        if total >= line_val + margin:
            return -0.12, note + " → fav over"
    return 0.0, note


def _weather_bias(features: dict[str, float], market: str, side: str) -> tuple[float, str]:
    if "goal" not in market:
        return 0.0, ""
    w = features.get("weather_goal_factor")
    if w is None:
        return 0.0, ""
    if w < 0.95:
        return (0.06 if side == "under" else -0.06), f"weather_goal_factor {w:.2f} (suppress goals)"
    if w > 1.02:
        return (0.04 if side == "over" else -0.04), f"weather_goal_factor {w:.2f} (open game)"
    return 0.0, f"weather_goal_factor {w:.2f}"


def _style_bias(home_style: str, away_style: str, market: str, side: str) -> float:
    text = f"{home_style} {away_style}".casefold()
    bias = 0.0
    low_block = any(x in text for x in ("low block", "低位", "大巴", "compact", "sit deep", "dense"))
    high_tempo = any(x in text for x in ("high tempo", "high corner", "pressing", "高位", "边路"))
    if "corner" in market:
        if side == "under" and low_block:
            bias += 0.05
        if side == "over" and high_tempo and not low_block:
            bias += 0.04
        if side == "over" and low_block:
            bias -= 0.06
    if "goal" in market:
        if side == "under" and low_block:
            bias += 0.04
        if side == "over" and ("high-scoring" in text or "open" in text):
            bias += 0.03
    return bias


def _short_price_factor(odds: float) -> float:
    """How much of the directional signal is already priced into the odds.

    Odds 1.40 (implied ~71% pre-margin) → the market is very confident in the
    favourite side of this market; directional biases there carry almost no
    independent information.  Odds 1.80+ → the market is near a coin-flip;
    model-side evidence still has room to be right.
    """
    if odds <= 0:
        return 1.0
    implied = 1.0 / odds
    # Map implied prob 0.50..0.75 → 1.0..0.2 (clamped).
    return max(0.2, min(1.0, (0.75 - implied) / 0.25))


def _market_aware_composite(
    *,
    market: str,
    side: str,
    odds: float,
    ev_base: float,
    model_p: float,
    market_p: float | None,
    fb: float,
    xb: float,
    wb: float,
    sb: float,
) -> float:
    """Composite = disagreement with the market minus consensus hype.

    Re-anchored 2026-08-22 after the live 08-14..21 window showed the legacy
    additive composite was *anti*-predictive.  High composite must mean GOOD:

      composite = ev_base                 (model EV at market price)
                + 0.5 * disagreement      (model_p - market_p) * odds
                + price-deflated directional (fb/xb/wb/sb)
                - consensus hype          (aligned signals at a short price)
    """
    disagreement = 0.0
    if market_p is not None and market_p > 0:
        disagreement = (model_p - market_p) * odds

    f = _short_price_factor(odds)
    directional = (fb + xb + wb + sb) * f

    composite = ev_base + 0.5 * disagreement + directional

    agreeing = sum(1 for b in (fb, xb, sb) if b >= 0.04)
    if agreeing >= 2 and odds <= 1.60:
        composite -= 0.08 * agreeing

    if "goal" in market and side == "over" and xb < -0.05:
        composite = min(composite, 0.28)
    return composite


@dataclass
class ScoredLine:
    opp: BetOpportunity
    old_p: float
    old_ev: float
    revised_p: float
    revised_ev: float
    revised_mu: float | None
    model_side: str | None
    market_p: float | None
    market_ev: float
    form_bias: float
    form_depth: int
    xg_bias: float
    weather_bias: float
    style_bias: float
    composite: float
    form_note: str
    xg_note: str
    weather_note: str
    reasons: list[str]


def score_line(
    opp: BetOpportunity,
    *,
    match: dict[str, Any],
    train: list[dict[str, Any]],
    form: dict[str, Any],
    features: dict[str, float],
    home_style: str,
    away_style: str,
    market_p: float | None,
) -> ScoredLine | None:
    market = opp.market
    stat = "corners" if "corner" in market else "goals" if "goal" in market else None
    period = "half_time" if market.endswith("_ht") else "full_time" if stat else None

    if stat:
        dc_state = None
        if USE_DC_MODEL:
            dc_state = fit_dc_cached(train, stat=stat, period=period, ref_date=match_ref_date(match))
        if dc_state is not None:
            team_role = team_role_from_market(market)
            if team_role:
                mus = predict_mus(dc_state, match)
                mu = (mus[0] if team_role == "home" else mus[1]) if mus else None
                p_over = predict_side_probability_dc(dc_state, match, line_raw=opp.line, side="over", team_role=team_role)
                p_under = predict_side_probability_dc(dc_state, match, line_raw=opp.line, side="under", team_role=team_role)
            else:
                mu = predict_total_mu(dc_state, match)
                p_over = predict_side_probability_dc(dc_state, match, line_raw=opp.line, side="over")
                p_under = predict_side_probability_dc(dc_state, match, line_raw=opp.line, side="under")
            state = None
        else:
            state = fit_poisson_total(train, stat=stat, period=period)
            if state is None:
                return None
            team_role = team_role_from_market(market)
            if team_role:
                mu = predict_team_mu(state, match, team_role=team_role)
                p_over = predict_side_probability(state, match, line_raw=opp.line, side="over", team_role=team_role)
                p_under = predict_side_probability(state, match, line_raw=opp.line, side="under", team_role=team_role)
            else:
                mu = predict_match_mu(state, match)
                p_over = predict_side_probability(state, match, line_raw=opp.line, side="over")
                p_under = predict_side_probability(state, match, line_raw=opp.line, side="under")
        if p_over >= p_under:
            model_side = "over"
            old_p = p_over if opp.side == "over" else p_under
        else:
            model_side = "under"
            old_p = p_under if opp.side == "under" else p_over
        old_ev = old_p * opp.decimal_odds - 1.0
        revised_p, revised_ev, revised_model_side = _revised_side_ev(match, {"market": market, "line": opp.line, "side": opp.side, "odds": opp.decimal_odds}, train)
        if revised_p is None:
            revised_p, revised_ev = old_p, old_ev
            revised_model_side = model_side
    else:
        mu = None
        model_side = None
        old_p = 0.0
        old_ev = 0.0
        revised_p, revised_ev, revised_model_side = None, None, None
        adapters = {a.key: a for a in get_adapters(["match_1x2", "match_1x2_ht"])}
        adapter = adapters.get(market)
        if adapter:
            train_m = adapter.training_matches(train)
            state = adapter.fit(train_m) if train_m else None
            if state:
                old_p = adapter.predict(state, opp, match)
                old_ev = old_p * opp.decimal_odds - 1.0
                revised_p, revised_ev = old_p, old_ev

    hx = features.get("home_xg")
    ax = features.get("away_xg")
    xg_effective = (
        _xg_expected_total(market, float(hx), float(ax))
        if "goal" in market and hx is not None and ax is not None
        else None
    )

    fb = _form_bias(form, market, opp.line, opp.side, xg_effective=xg_effective)
    depth = _form_sample_depth(form, market)
    if depth < 2 and ("corner" in market or "goal" in market):
        fb -= 0.10 if opp.side == "over" else 0.04
    xb, xn = _xg_bias(features, market, opp.line, opp.side)
    wb, wn = _weather_bias(features, market, opp.side)
    sb = _style_bias(home_style, away_style, market, opp.side)
    fn = style_note_from_form(form, {"market": market, "side": opp.side})

    ev_base = revised_ev if revised_ev is not None else old_ev
    if MARKET_AWARE_COMPOSITE:
        composite = _market_aware_composite(
            market=market,
            side=opp.side,
            odds=opp.decimal_odds,
            ev_base=ev_base,
            model_p=(revised_p if revised_p is not None else old_p),
            market_p=market_p,
            fb=fb,
            xb=xb,
            wb=wb,
            sb=sb,
        )
    else:
        composite = ev_base + fb + xb + wb + sb
        if xb >= 0.08 and wb >= 0.04:
            composite += 0.12
        if "goal" in market and opp.side == "over" and xb < -0.05 and fb > 0:
            composite -= fb
        # Poisson μ can hallucinate; BSD xG caps composite on contradicting goal overs.
        if "goal" in market and opp.side == "over" and xb < -0.05:
            composite = min(composite, 0.28)
    reasons: list[str] = []
    if depth < 2 and ("corner" in market or "goal" in market):
        reasons.append(f"thin form sample (n={depth})")
    if fn:
        reasons.append(fn)
    if xn:
        reasons.append(xn)
    if wn:
        reasons.append(wn)
    if "goal" in market and opp.side == "over" and xb < -0.05 and fn and "high-scoring" in fn:
        reasons.append("BSD xG below line overrides high-scoring form")
    if revised_model_side and revised_model_side != opp.side:
        reasons.append(f"model prefers {revised_model_side}")
    if fb > 0.05:
        reasons.append("form supports side")
    elif fb < -0.05:
        reasons.append("form conflicts")

    mkt_ev = (market_p * opp.decimal_odds - 1.0) if market_p is not None else -0.075

    return ScoredLine(
        opp=opp,
        old_p=round(old_p, 4),
        old_ev=round(old_ev, 4),
        revised_p=round(revised_p, 4) if revised_p is not None else "",
        revised_ev=round(revised_ev, 4) if revised_ev is not None else "",
        revised_mu=round(mu, 3) if mu is not None else "",
        model_side=revised_model_side or model_side,
        market_p=round(market_p, 4) if market_p is not None else None,
        market_ev=round(mkt_ev, 4),
        form_bias=round(fb, 3),
        form_depth=depth,
        xg_bias=round(xb, 3),
        weather_bias=round(wb, 3),
        style_bias=round(sb, 3),
        composite=round(composite, 4),
        form_note=fn,
        xg_note=xn,
        weather_note=wn,
        reasons=reasons,
    )


def _passes_filters(sc: ScoredLine) -> bool:
    market = sc.opp.market
    if "corner" in market or "goal" in market:
        # A recommendation that contradicts its own model is a bug, not a
        # contrarian signal. 07-09/10: both such lines lost.
        if _is_model_conflict(sc):
            return False
        # Model errors are largest where the market price is longest.  Do not
        # manufacture apparent edge from a high-priced goal-over probability.
        if _is_high_odds_goal_over(sc.opp):
            return False
        if sc.xg_bias <= -0.08:
            return False
        if sc.form_bias <= -0.08:
            return False
        if sc.weather_bias < 0 and sc.xg_bias < 0:
            return False
        if "goal" in market and sc.opp.side == "over" and sc.xg_bias < 0:
            return False
        # 07-04→08: goal HT over 8-17 (-38% ROI); require xG support or very high composite.
        if market in ("goal_ou_ht", "goal_ou_home_ht", "goal_ou_away_ht") and sc.opp.side == "over":
            if sc.xg_bias < 0.02 and sc.composite < 0.70:
                return False
        # Overconfident Poisson (p>=0.85) on goal overs: 2-6, -62% ROI.
        if "goal" in market and sc.opp.side == "over" and sc.old_p >= 0.85:
            if sc.xg_bias < 0.05:
                return False
        if "corner" in market and sc.opp.side == "over" and sc.form_depth == 0:
            return False
        if sc.form_depth < 2 and sc.opp.side == "over" and sc.composite < 0.25:
            return False
        if sc.revised_p != "" and sc.revised_p < 0.45 and sc.composite < 0.12:
            return False
        if sc.old_ev > 0.20 and sc.composite > 0.0 and sc.xg_bias < 0:
            return False
        return sc.composite > 0.03 or (sc.old_ev > 0.20 and sc.composite > 0.0 and sc.xg_bias >= 0)
    return sc.composite > 0.04 or sc.old_ev > 0.10


def _tier(
    sc: ScoredLine,
    *,
    is_seeded: bool = True,
    match_seeded: bool = True,
    policy: str = POLICY_VERSION,
) -> str:
    """Classify a scored line. Live BET is gated by policy paper rules."""
    market = sc.opp.market
    side = sc.opp.side
    paper = _paper_reason(sc, is_seeded=is_seeded, match_seeded=match_seeded, policy=policy)
    if paper is not None:
        return "PAPER"
    # Goal HT overs need stronger composite for full BET tier (historically noisy).
    bet_floor = 0.10
    if market in ("goal_ou_ht", "goal_ou_home_ht", "goal_ou_away_ht") and side == "over":
        bet_floor = 0.55
    if sc.composite >= bet_floor and (sc.revised_ev == "" or sc.revised_ev >= 0.05 or sc.old_ev >= 0.20):
        return "BET"
    if sc.composite >= 0.04 and (sc.old_ev > 0 or sc.revised_ev == "" or sc.revised_ev > 0):
        return "BET*"
    return "PASS"


def _opp_key(sc: ScoredLine) -> tuple[str, str, str]:
    return (sc.opp.market, sc.opp.line, sc.opp.side)


def revise(
    *,
    original_bets: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    history: list[dict[str, Any]],
    features_map: dict[str, dict[str, float]],
    play_styles: dict[str, str],
    match_ids: set[str] | None = None,
    policy: str = POLICY_VERSION,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    adapters = get_adapters(DEFAULT_MARKET_KEYS)
    targets_by_id = {m["match_id"]: m for m in targets}
    orig_by_mid: dict[str, list[dict[str, Any]]] = {}
    for b in original_bets:
        orig_by_mid.setdefault(b["match_id"], []).append(b)

    ids = match_ids or set(orig_by_mid)
    all_revised: list[dict[str, Any]] = []
    change_log: list[dict[str, Any]] = []

    for mid in sorted(ids):
        match = targets_by_id.get(mid)
        if match is None:
            continue
        cutoff = parse_match_date(match["date"])
        train = [m for m in history if parse_match_date(m["date"]) < cutoff]
        form = match_form_summary(history, match, before=cutoff, limit=5)
        features = features_map.get(mid, {})
        if isinstance(match.get("external_features"), dict):
            features = {**features, **match["external_features"]}
        pair = split_teams(match.get("teams") or "") or ("", "")
        home_style = play_styles.get(pair[0], "")
        away_style = play_styles.get(pair[1], "")

        scored: list[ScoredLine] = []
        for adapter in adapters:
            opps = adapter.extract_opportunities(match)
            groups: dict[tuple[str, str], list[BetOpportunity]] = {}
            for opp in opps:
                groups.setdefault((opp.market, opp.line), []).append(opp)
            for group in groups.values():
                for opp in group:
                    mp = _market_p(opp, group)
                    sc = score_line(
                        opp,
                        match=match,
                        train=train,
                        form=form,
                        features=features,
                        home_style=home_style,
                        away_style=away_style,
                        market_p=mp,
                    )
                    if sc and _passes_filters(sc):
                        scored.append(sc)

        scored.sort(key=lambda s: (-s.composite, -s.market_ev))
        best_by_line: dict[tuple[str, str], ScoredLine] = {}
        for sc in scored:
            key = (sc.opp.market, sc.opp.line)
            prev = best_by_line.get(key)
            if prev is None or sc.composite > prev.composite:
                best_by_line[key] = sc

        selected = sorted(best_by_line.values(), key=lambda s: (-s.composite, -s.market_ev))[:5]

        orig_keys = {
            (b["market"], b["line"], b["side"]): b for b in orig_by_mid.get(mid, [])
        }
        orig_key_set = set(orig_keys)
        match_seeded = bool(orig_key_set)
        selected_tiers: dict[tuple[str, str, str], tuple[ScoredLine, str, str | None]] = {}
        for sc in selected:
            key = _opp_key(sc)
            seeded = _is_orig_keep_or_flip(key, orig_key_set)
            paper = _paper_reason(sc, is_seeded=seeded, match_seeded=match_seeded, policy=policy)
            tier = _tier(sc, is_seeded=seeded, match_seeded=match_seeded, policy=policy)
            if tier in ("BET", "BET*", "PAPER"):
                selected_tiers[key] = (sc, tier, paper)

        for key, ob in orig_keys.items():
            if key not in selected_tiers:
                flipped = next(
                    (
                        sc
                        for nk, (sc, _tier_name, _paper) in selected_tiers.items()
                        if nk[0] == key[0] and nk[1] == key[1] and nk[2] != key[2]
                    ),
                    None,
                )
                if flipped:
                    change_log.append(
                        {
                            "match_id": mid,
                            "action": "FLIP",
                            "pick": _fmt_pick(ob),
                            "to": _fmt_pick(
                                {
                                    "market": flipped.opp.market,
                                    "line": flipped.opp.line,
                                    "side": flipped.opp.side,
                                    "odds": flipped.opp.decimal_odds,
                                }
                            ),
                            "reason": "; ".join(flipped.reasons),
                        }
                    )
                else:
                    change_log.append(
                        {
                            "match_id": mid,
                            "action": "DROP",
                            "pick": _fmt_pick(ob),
                            "reason": "failed composite / form / xG filters after revision",
                        }
                    )
        for key, (sc, tier, paper) in selected_tiers.items():
            seeded = _is_orig_keep_or_flip(key, orig_key_set)
            reasons = list(sc.reasons)
            if paper:
                reasons.append(paper)
            reasons.append(f"policy={policy}")
            row = {
                "date": sc.opp.date.isoformat(),
                "match_id": mid,
                "teams": sc.opp.teams,
                "competition": sc.opp.competition or "",
                "market": sc.opp.market,
                "line": sc.opp.line,
                "side": sc.opp.side,
                "odds": sc.opp.decimal_odds,
                "market_p": sc.market_p,
                "market_ev": sc.market_ev,
                "old_p": sc.old_p,
                "old_ev": sc.old_ev,
                "bet": tier,
                "model_mu": sc.revised_mu,
                "model_p_side": sc.revised_p,
                "model_ev_side": sc.revised_ev,
                "model_side": sc.model_side,
                "composite_score": sc.composite,
                "form_bias": sc.form_bias,
                "xg_bias": sc.xg_bias,
                "weather_bias": sc.weather_bias,
                "style_bias": sc.style_bias,
                "policy": policy,
                "revision_reason": "; ".join(reasons),
                "pick": _fmt_pick(
                    {
                        "market": sc.opp.market,
                        "line": sc.opp.line,
                        "side": sc.opp.side,
                        "odds": sc.opp.decimal_odds,
                    }
                ),
            }
            if key in orig_keys:
                row["action"] = "PAPER" if tier == "PAPER" else "KEEP" if tier == orig_keys[key].get("bet") else "KEEP*"
            elif seeded:
                row["action"] = "FLIP" if tier != "PAPER" else "PAPER"
            elif match_seeded and sc.opp.market in {"goal_ou_ft", "goal_ou_ht"} and sc.opp.side == "under" and tier == "BET":
                row["action"] = "UPGRADE"
            else:
                row["action"] = "PAPER" if tier == "PAPER" else "NEW"
            change_log.append(
                {"match_id": mid, "action": row["action"], "pick": row["pick"], "reason": row["revision_reason"]}
            )
            if tier == "BET":
                all_revised.append(row)
            elif tier == "PAPER":
                row["bet"] = "PAPER"
                row["paper_trade"] = True
                all_revised.append(row)
            elif tier == "BET*":
                row["bet"] = "BET*"
                row["action"] = "NEW*" if not seeded else "KEEP*"
                all_revised.append(row)

    all_revised.sort(key=lambda r: (-r["composite_score"], r["match_id"], r["market"]))
    return all_revised, change_log


def _patch_match_md(mid: str, revised_rows: list[dict[str, Any]], out_dir: Path, drops: list[dict[str, Any]] | None = None) -> None:
    path = out_dir / f"{mid}.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    block_lines = [
        "",
        "## Revised Recommendation (form + xG + weather + style)",
        "",
        "| Action | Pick | Composite | Rev EV | Form | xG | Weather | Reason |",
        "|--------|------|-----------|--------|------|-----|---------|--------|",
    ]
    rows = [r for r in revised_rows if r["match_id"] == mid]
    if not rows and drops:
        for d in drops:
            if d.get("match_id") != mid or d.get("action") != "DROP":
                continue
            block_lines.append(
                f"| DROP | {d.get('pick','')} | — | — | — | — | — | {d.get('reason','')[:80]} |"
            )
    for r in rows:
        block_lines.append(
            f"| {r.get('action','')} | {r['pick']} | {r['composite_score']} | {r.get('model_ev_side','')} | "
            f"{r.get('form_bias','')} | {r.get('xg_bias','')} | {r.get('weather_bias','')} | {r.get('revision_reason','')[:80]} |"
        )
    if not rows and not drops:
        block_lines.append("| PASS | — | — | — | — | — | — | no line passed revision filters |")
    block = "\n".join(block_lines) + "\n"
    marker = "## Revised Recommendation"
    if marker in text:
        text = text.split(marker)[0].rstrip() + block
    else:
        if "---" in text:
            parts = text.rsplit("---", 1)
            text = parts[0].rstrip() + block + "\n---" + parts[1]
        else:
            text = text.rstrip() + block
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revise BET picks with form/xG/weather/style")
    parser.add_argument("--original", type=Path, default=ROOT / "output" / "tomorrow_20260704_newmodel_bet_only.json")
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--features", type=Path, default=ROOT / "output" / "external_features_bsd_en.json")
    parser.add_argument("--styles", type=Path, default=ROOT / "output" / "play_styles_all_20260704.json")
    parser.add_argument("--out-json", type=Path, default=ROOT / "output" / "tomorrow_20260704_revised_bets.json")
    parser.add_argument(
        "--out-paper-json",
        type=Path,
        default=None,
        help="Optional output for team-goal paper trades; never included in live bets.",
    )
    parser.add_argument("--out-csv", type=Path, default=ROOT / "output" / "tomorrow_20260704_revised_bets.csv")
    parser.add_argument("--changes-json", type=Path, default=ROOT / "output" / "tomorrow_20260704_revision_log.json")
    parser.add_argument("--analysis-dir", type=Path, default=ROOT / "output" / "match_analysis_20260704")
    parser.add_argument("--patch-analysis", action="store_true", default=True)
    args = parser.parse_args(argv)

    original = json.loads(args.original.read_text(encoding="utf-8"))
    snapshot = args.snapshot or latest_snapshot(date="2026-07-03")
    targets = load_target_matches(snapshot) if snapshot else []
    if args.features.exists():
        targets = merge_external_features(targets, load_feature_map(args.features))
    play_styles = json.loads(args.styles.read_text(encoding="utf-8")) if args.styles.exists() else {}
    features_map = load_feature_map(args.features) if args.features.exists() else {}
    history = load_matches()

    revised, changes = revise(
        original_bets=original,
        targets=targets,
        history=history,
        features_map=features_map,
        play_styles=play_styles,
    )

    bet_only = [r for r in revised if r["bet"] == "BET"]
    paper_only = [r for r in revised if r["bet"] == "PAPER"]
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(bet_only, ensure_ascii=False, indent=2), encoding="utf-8")
    paper_path = args.out_paper_json or args.out_json.with_name(f"{args.out_json.stem}_paper.json")
    paper_path.write_text(json.dumps(paper_only, ensure_ascii=False, indent=2), encoding="utf-8")
    args.changes_json.write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")

    if bet_only:
        with args.out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(bet_only[0].keys()))
            w.writeheader()
            w.writerows(bet_only)

    if args.patch_analysis:
        bet_by_mid: dict[str, list[dict[str, Any]]] = {}
        for r in bet_only:
            bet_by_mid.setdefault(r["match_id"], []).append(r)
        touched: set[str] = set(bet_by_mid)
        touched.update(c["match_id"] for c in changes if c.get("match_id"))
        for mid in touched:
            _patch_match_md(
                mid,
                bet_only,
                args.analysis_dir,
                drops=[c for c in changes if c.get("match_id") == mid and c.get("action") == "DROP"],
            )

    drops = sum(1 for c in changes if c["action"] == "DROP")
    news = sum(1 for c in changes if c["action"].startswith("NEW"))
    print(f"Revised BET: {len(bet_only)} (was {len(original)}) · paper={len(paper_only)} · drops={drops} · new={news}")
    print(f"-> {args.out_json}")
    print(f"-> {paper_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
