"""Forward bet recommendations for manually supplied match odds."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.betting.devig import implied_multi_way, implied_over_under
from pipeline.betting.enrich_competition import enrich_match_competitions
from pipeline.betting.load import parse_match_date
from pipeline.betting.markets import get_adapters
from pipeline.betting.types import BetOpportunity, ModelState
from pipeline.config import DEFAULT_DB, DEFAULT_RECORDS_JSON


@dataclass(frozen=True)
class RecommendationConfig:
    market_keys: list[str]
    min_ev: float = 0.05
    min_edge: float = 0.0
    min_train_matches: int = 200
    best_per_match: bool = False
    collapse_related: bool = True
    kelly_fraction: float = 0.25
    max_stake_fraction: float = 0.02
    bankroll: float | None = None


@dataclass(frozen=True)
class BetRecommendation:
    date: date
    match_id: str
    teams: str
    competition: str | None
    market: str
    line: str
    side: str
    odds: float
    p_model: float
    ev: float
    p_implied: float | None
    edge: float | None
    breakeven_odds: float | None
    odds_edge: float | None
    train_size: int
    recommendation: str
    stake_fraction: float
    stake_amount: float | None

    def to_row(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "match_id": self.match_id,
            "teams": self.teams,
            "competition": self.competition or "",
            "market": self.market,
            "line": self.line,
            "side": self.side,
            "odds": round(self.odds, 4),
            "p_model": round(self.p_model, 4),
            "p_implied": round(self.p_implied, 4) if self.p_implied is not None else "",
            "edge": round(self.edge, 4) if self.edge is not None else "",
            "breakeven_odds": round(self.breakeven_odds, 4) if self.breakeven_odds is not None else "",
            "odds_edge": round(self.odds_edge, 4) if self.odds_edge is not None else "",
            "ev": round(self.ev, 4),
            "support_matches": self.train_size,
            "stake_fraction": round(self.stake_fraction, 4),
            "stake_amount": round(self.stake_amount, 2) if self.stake_amount is not None else "",
            "recommendation": self.recommendation,
        }


def load_target_matches(
    path: Path,
    *,
    enrich_competition: bool = True,
    records_path: Path = DEFAULT_RECORDS_JSON,
    db_path: Path = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Load one match, a list of matches, or a records-like {matches: [...]} file."""
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict) and isinstance(payload.get("matches"), list):
        matches = payload["matches"]
    elif isinstance(payload, dict):
        matches = [payload]
    else:
        raise ValueError(f"Unsupported target match JSON shape: {path}")

    matches = [m for m in matches if isinstance(m, dict)]
    if enrich_competition:
        matches = enrich_match_competitions(matches, records_path=records_path, db_path=db_path)
    return matches


def _history_before(matches: list[dict[str, Any]], target_date: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in matches:
        try:
            if parse_match_date(match["date"]) < target_date:
                out.append(match)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _opportunity_implied(
    opportunity: BetOpportunity,
    market_opportunities: list[BetOpportunity],
) -> float | None:
    if opportunity.side in ("over", "under"):
        return implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)

    if opportunity.line == "1x2":
        odds_by_side = {
            opp.side: opp.decimal_odds
            for opp in market_opportunities
            if opp.line == opportunity.line and opp.decimal_odds > 0
        }
        return implied_multi_way(odds_by_side, opportunity.side)

    return None


def _score_opportunities(
    *,
    match: dict[str, Any],
    opportunities: list[BetOpportunity],
    state: ModelState,
    adapter,
    train_size: int,
    config: RecommendationConfig,
) -> list[BetRecommendation]:
    scored: list[BetRecommendation] = []
    expected_roi = getattr(adapter, "expected_roi", None)
    fair_odds = getattr(adapter, "fair_odds", None)
    for opportunity in opportunities:
        p_model = adapter.predict(state, opportunity, match)
        ev = (
            float(expected_roi(state, opportunity, match))
            if expected_roi is not None
            else p_model * opportunity.decimal_odds - 1.0
        )
        p_implied = _opportunity_implied(opportunity, opportunities)
        edge = (p_model - p_implied) if p_implied is not None else None
        breakeven_odds = (
            float(fair_odds(state, opportunity, match))
            if fair_odds is not None
            else (1.0 / p_model if p_model > 0 else None)
        )
        odds_edge = (opportunity.decimal_odds - breakeven_odds) if breakeven_odds is not None else None
        full_kelly = ev / (opportunity.decimal_odds - 1.0) if ev > 0 and opportunity.decimal_odds > 1.0 else 0.0
        stake_fraction = min(
            max(0.0, full_kelly * config.kelly_fraction),
            max(0.0, config.max_stake_fraction),
        )
        stake_amount = (stake_fraction * config.bankroll) if config.bankroll is not None else None
        scored.append(
            BetRecommendation(
                date=opportunity.date,
                match_id=opportunity.match_id,
                teams=opportunity.teams,
                competition=opportunity.competition,
                market=opportunity.market,
                line=opportunity.line,
                side=opportunity.side,
                odds=opportunity.decimal_odds,
                p_model=p_model,
                ev=ev,
                p_implied=p_implied,
                edge=edge,
                breakeven_odds=breakeven_odds,
                odds_edge=odds_edge,
                train_size=train_size,
                recommendation="BET" if ev > 0 else "PASS",
                stake_fraction=stake_fraction,
                stake_amount=stake_amount,
            )
        )
    return scored


def _best_per_match(records: list[BetRecommendation]) -> list[BetRecommendation]:
    best: dict[tuple[date, str], BetRecommendation] = {}
    for record in records:
        key = (record.date, record.match_id)
        if key not in best or record.ev > best[key].ev:
            best[key] = record
    return sorted(best.values(), key=lambda r: (-r.ev, r.date, r.match_id, r.market, r.line, r.side))


def _main_line_score(record: BetRecommendation) -> float:
    if record.p_implied is None:
        return 1.0
    return abs(record.p_implied - 0.5)


def _collapse_related(records: list[BetRecommendation]) -> list[BetRecommendation]:
    """Keep one representative line per match/market/side before final ranking."""
    best: dict[tuple[date, str, str, str], BetRecommendation] = {}
    for record in records:
        key = (record.date, record.match_id, record.market, record.side)
        current = best.get(key)
        if current is None:
            best[key] = record
            continue
        current_rank = (_main_line_score(current), -current.ev)
        candidate_rank = (_main_line_score(record), -record.ev)
        if candidate_rank < current_rank:
            best[key] = record
    return sorted(best.values(), key=lambda r: (-r.ev, r.date, r.match_id, r.market, r.line, r.side))


def recommend_bets(
    *,
    history_matches: list[dict[str, Any]],
    target_matches: list[dict[str, Any]],
    config: RecommendationConfig,
) -> list[BetRecommendation]:
    adapters = get_adapters(config.market_keys)
    state_cache: dict[tuple[date, str], tuple[ModelState | None, int]] = {}
    records: list[BetRecommendation] = []

    for match in target_matches:
        target_date = parse_match_date(match["date"])
        train_pool = _history_before(history_matches, target_date)

        for adapter in adapters:
            cache_key = (target_date, adapter.key)
            if cache_key not in state_cache:
                train_matches = adapter.training_matches(train_pool)
                state = adapter.fit(train_matches) if len(train_matches) >= config.min_train_matches else None
                state_cache[cache_key] = (state, len(train_matches))

            state, train_size = state_cache[cache_key]
            if state is None:
                continue

            opportunities = adapter.extract_opportunities(match)
            if not opportunities:
                continue

            for record in _score_opportunities(
                match=match,
                opportunities=opportunities,
                state=state,
                adapter=adapter,
                train_size=train_size,
                config=config,
            ):
                if record.ev < config.min_ev:
                    continue
                if record.edge is None or record.edge < config.min_edge:
                    continue
                records.append(record)

    records.sort(key=lambda r: (-r.ev, r.date, r.match_id, r.market, r.line, r.side))
    if config.collapse_related:
        records = _collapse_related(records)
    if config.best_per_match:
        return _best_per_match(records)
    return records


def recommendations_to_rows(records: list[BetRecommendation]) -> list[dict[str, Any]]:
    return [record.to_row() for record in records]


def write_recommendations_csv(records: list[BetRecommendation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = recommendations_to_rows(records)
    fieldnames = [
        "date",
        "match_id",
        "teams",
        "competition",
        "market",
        "line",
        "side",
        "odds",
        "p_model",
        "p_implied",
        "edge",
        "breakeven_odds",
        "odds_edge",
        "ev",
        "support_matches",
        "stake_fraction",
        "stake_amount",
        "recommendation",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
