"""Walk-forward +EV backtest engine."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from pipeline.betting.devig import implied_over_under
from pipeline.betting.load import parse_match_date
from pipeline.betting.markets import get_adapters
from pipeline.betting.settlement import pnl_for_outcome, pnl_over_under
from pipeline.betting.types import BetOpportunity, BetOutcome, BetRecord, ModelState


@dataclass
class BacktestConfig:
    market_keys: list[str]
    min_ev: float = 0.05
    min_train_matches: int = 200
    stake: float = 1.0


def _get_total_for_market(adapter, match: dict[str, Any]) -> int | None:
    if hasattr(adapter, "period"):
        from pipeline.betting.markets.corner_ou import _corner_total
        from pipeline.betting.markets.goal_ou import _goal_total

        if adapter.key.startswith("corner_"):
            return _corner_total(match, adapter.period)
        if adapter.key.startswith("goal_"):
            return _goal_total(match, adapter.period)
    return None


def _compute_pnl(
    adapter,
    opportunity: BetOpportunity,
    match: dict[str, Any],
    outcome: BetOutcome,
    stake: float,
) -> float:
    total = _get_total_for_market(adapter, match)
    if total is not None and opportunity.side in ("over", "under"):
        _, pnl = pnl_over_under(total, opportunity.line, opportunity.side, opportunity.decimal_odds, stake)
        return pnl
    return pnl_for_outcome(outcome, opportunity.decimal_odds, stake)


def _compute_ev(adapter, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any], p_model: float) -> float:
    expected_roi = getattr(adapter, "expected_roi", None)
    if expected_roi is not None:
        return float(expected_roi(state, opportunity, match))
    return p_model * opportunity.decimal_odds - 1.0


def run_backtest(
    matches: list[dict[str, Any]],
    config: BacktestConfig,
) -> list[BetRecord]:
    adapters = get_adapters(config.market_keys)
    adapter_by_key = {a.key: a for a in adapters}

    matches_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        matches_by_date[parse_match_date(match["date"])].append(match)

    opportunities_by_date_market: dict[tuple[date, str], list[tuple[BetOpportunity, dict[str, Any]]]] = defaultdict(list)
    for match in matches:
        for adapter in adapters:
            for opp in adapter.extract_opportunities(match):
                opportunities_by_date_market[(opp.date, adapter.key)].append((opp, match))

    train_by_market: dict[str, list[dict[str, Any]]] = {a.key: [] for a in adapters}
    records: list[BetRecord] = []

    for current_date in sorted(matches_by_date.keys()):
        models: dict[str, ModelState | None] = {}
        train_sizes: dict[str, int] = {}
        for adapter in adapters:
            train = train_by_market[adapter.key]
            train_sizes[adapter.key] = len(train)
            if len(train) >= config.min_train_matches:
                models[adapter.key] = adapter.fit(train)
            else:
                models[adapter.key] = None

        for market_key in config.market_keys:
            adapter = adapter_by_key[market_key]
            state = models[market_key]
            train_size = train_sizes[market_key]
            if state is None:
                continue

            for opportunity, match in opportunities_by_date_market.get((current_date, market_key), []):
                p_model = adapter.predict(state, opportunity, match)
                ev = _compute_ev(adapter, state, opportunity, match, p_model)
                if ev < config.min_ev:
                    continue

                p_implied = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
                edge = (p_model - p_implied) if p_implied is not None else None

                outcome = adapter.settle(opportunity, match)
                if outcome == BetOutcome.UNKNOWN:
                    continue

                pnl = _compute_pnl(adapter, opportunity, match, outcome, config.stake)
                records.append(
                    BetRecord(
                        opportunity=opportunity,
                        p_model=p_model,
                        ev=ev,
                        p_implied=p_implied,
                        edge_vs_close=edge,
                        outcome=outcome,
                        pnl=pnl,
                        train_size=train_size,
                    )
                )

        # Append today's settled training rows for tomorrow's fit.
        for match in matches_by_date[current_date]:
            for adapter in adapters:
                if match in adapter.training_matches([match]):
                    train_by_market[adapter.key].append(match)

    return records


@dataclass
class ComparisonRow:
    """One settleable opportunity scored under raw / market / calibrated models."""

    opportunity: BetOpportunity
    outcome: BetOutcome
    pnl: float
    train_size: int
    p_raw: float
    p_market: float | None
    p_final: float
    ev_raw: float
    ev_market: float | None
    ev_final: float
    blend_weight: float | None
    calibration_n: int | None


def _score_summary(rows: list[ComparisonRow], *, mode: str, min_ev: float) -> dict[str, Any]:
    selected: list[ComparisonRow] = []
    for row in rows:
        if mode == "raw":
            p, ev = row.p_raw, row.ev_raw
        elif mode == "market":
            if row.p_market is None or row.ev_market is None:
                continue
            p, ev = row.p_market, row.ev_market
        else:
            p, ev = row.p_final, row.ev_final
        if ev < min_ev:
            continue
        selected.append(row)

    settled = [r for r in selected if r.outcome in (BetOutcome.WIN, BetOutcome.LOSE, BetOutcome.PUSH)]
    wins = sum(1 for r in settled if r.outcome == BetOutcome.WIN)
    losses = sum(1 for r in settled if r.outcome == BetOutcome.LOSE)
    pushes = sum(1 for r in settled if r.outcome == BetOutcome.PUSH)
    pnl = sum(r.pnl for r in settled)
    stake = float(len(settled))
    # Reliability only on binary settle (ignore pushes for Brier/log-loss).
    binary = [(r, 1.0 if r.outcome == BetOutcome.WIN else 0.0) for r in settled if r.outcome in (BetOutcome.WIN, BetOutcome.LOSE)]
    brier = 0.0
    log_loss = 0.0
    if binary:
        import math

        for r, y in binary:
            if mode == "raw":
                p = r.p_raw
            elif mode == "market":
                p = float(r.p_market or 0.5)
            else:
                p = r.p_final
            p = min(1.0 - 1e-6, max(1e-6, p))
            brier += (p - y) ** 2
            log_loss += -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
        brier /= len(binary)
        log_loss /= len(binary)

    by_match: dict[str, float] = defaultdict(float)
    for r in settled:
        by_match[r.opportunity.match_id] += r.pnl

    return {
        "mode": mode,
        "n_bets": len(settled),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pnl": round(pnl, 4),
        "roi": round(pnl / stake, 4) if stake else 0.0,
        "brier": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "match_clustered_pnl": round(sum(by_match.values()), 4),
        "n_matches": len(by_match),
    }


def run_goal_model_comparison(
    matches: list[dict[str, Any]],
    *,
    market_keys: list[str] | None = None,
    min_train_matches: int = 100,
    min_ev: float = 0.0,
    stake: float = 1.0,
) -> dict[str, Any]:
    """Walk-forward compare raw Poisson totals vs market vs calibrated v5.

    Pending / unknown outcomes are excluded from settled ROI.
    """
    keys = market_keys or ["goal_ou_ft", "goal_ou_ht"]
    adapters = get_adapters(keys)
    adapter_by_key = {a.key: a for a in adapters}

    matches_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        matches_by_date[parse_match_date(match["date"])].append(match)

    opportunities_by_date_market: dict[tuple[date, str], list[tuple[BetOpportunity, dict[str, Any]]]] = defaultdict(list)
    for match in matches:
        for adapter in adapters:
            for opp in adapter.extract_opportunities(match):
                opportunities_by_date_market[(opp.date, adapter.key)].append((opp, match))

    train_by_market: dict[str, list[dict[str, Any]]] = {a.key: [] for a in adapters}
    rows: list[ComparisonRow] = []

    for current_date in sorted(matches_by_date.keys()):
        models: dict[str, ModelState | None] = {}
        train_sizes: dict[str, int] = {}
        for adapter in adapters:
            train = train_by_market[adapter.key]
            train_sizes[adapter.key] = len(train)
            models[adapter.key] = adapter.fit(train) if len(train) >= min_train_matches else None

        for market_key in keys:
            adapter = adapter_by_key[market_key]
            state = models[market_key]
            if state is None or "scoreline" not in state.data:
                continue
            train_size = train_sizes[market_key]
            predict_details = getattr(adapter, "predict_details", None)
            for opportunity, match in opportunities_by_date_market.get((current_date, market_key), []):
                outcome = adapter.settle(opportunity, match)
                if outcome == BetOutcome.UNKNOWN:
                    continue
                details = (
                    predict_details(state, opportunity, match)
                    if predict_details is not None
                    else {
                        "p_raw": adapter.predict(state, opportunity, match),
                        "p_market": implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side),
                        "p_final": adapter.predict(state, opportunity, match),
                        "blend_weight": None,
                        "calibration_n": None,
                    }
                )
                p_raw = float(details.get("p_raw") or 0.0)
                p_market = details.get("p_market")
                p_final = float(details.get("p_final") or p_raw)
                p_market_f = float(p_market) if p_market is not None else None
                ev_raw = p_raw * opportunity.decimal_odds - 1.0
                # Prefer adapter expected_roi for calibrated / quarter-correct EV.
                ev_final = _compute_ev(adapter, state, opportunity, match, p_final)
                ev_market = (p_market_f * opportunity.decimal_odds - 1.0) if p_market_f is not None else None
                pnl = _compute_pnl(adapter, opportunity, match, outcome, stake)
                rows.append(
                    ComparisonRow(
                        opportunity=opportunity,
                        outcome=outcome,
                        pnl=pnl,
                        train_size=train_size,
                        p_raw=p_raw,
                        p_market=p_market_f,
                        p_final=p_final,
                        ev_raw=ev_raw,
                        ev_market=ev_market,
                        ev_final=ev_final,
                        blend_weight=details.get("blend_weight"),  # type: ignore[arg-type]
                        calibration_n=details.get("calibration_n"),  # type: ignore[arg-type]
                    )
                )

        for match in matches_by_date[current_date]:
            for adapter in adapters:
                if match in adapter.training_matches([match]):
                    train_by_market[adapter.key].append(match)

    return {
        "n_scored": len(rows),
        "min_ev": min_ev,
        "min_train_matches": min_train_matches,
        "markets": keys,
        "raw": _score_summary(rows, mode="raw", min_ev=min_ev),
        "market": _score_summary(rows, mode="market", min_ev=min_ev),
        "calibrated_v5": _score_summary(rows, mode="calibrated", min_ev=min_ev),
        # Probability quality on every settleable opportunity (no +EV selection).
        # Market +EV is usually empty because of vig; this is the honest calibration view.
        "probability_all": {
            "raw": _score_summary(rows, mode="raw", min_ev=-999.0),
            "market": _score_summary(rows, mode="market", min_ev=-999.0),
            "calibrated_v5": _score_summary(rows, mode="calibrated", min_ev=-999.0),
        },
    }
