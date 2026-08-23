#!/usr/bin/env python3
"""Pick the higher model-probability side within each offered market line.

This is a direction tool, not a +EV recommendation tool. It uses model/fair
probabilities to choose between sides such as over vs under, even when both
sides are negative EV after bookmaker margin.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

from pipeline.betting.devig import devig_multi_way, implied_over_under
from pipeline.betting.load import load_matches, parse_match_date
from pipeline.betting.markets import DEFAULT_MARKETS, get_adapters, list_markets
from pipeline.betting.markets.team_ou import team_role_from_market
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.models.poisson_total import (
    expected_over_under_roi,
    fair_over_under_odds,
    predict_side_probability,
)
from pipeline.betting.models.score_poisson import ScorelineState, predict_1x2_probs
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.snapshots import latest_snapshot
from pipeline.betting.types import BetOpportunity, ModelState
from pipeline.config import DEFAULT_RECORDS_JSON


def _history_before(matches: list[dict[str, Any]], target: Any) -> list[dict[str, Any]]:
    return [m for m in matches if parse_match_date(m["date"]) < target]


def _group_key(opp: BetOpportunity) -> tuple[str, str]:
    return (opp.market, opp.line)


def _market_probability(opp: BetOpportunity, group: list[BetOpportunity]) -> float | None:
    if opp.side in ("over", "under"):
        return implied_over_under(opp.over_odds, opp.under_odds, opp.side)
    odds_by_side = {item.side: item.decimal_odds for item in group}
    return devig_multi_way(odds_by_side).get(opp.side)


def _model_probability(adapter: Any, state: ModelState, opp: BetOpportunity, match: dict[str, Any]) -> float:
    if opp.side in ("over", "under") and "scoreline" in state.data:
        # v5 calibrated goal totals: adapter.predict returns p_final.
        return adapter.predict(state, opp, match)
    if opp.side in ("over", "under") and "poisson" in state.data:
        team_role = state.data.get("team_role") or team_role_from_market(opp.market)
        return predict_side_probability(
            state.data["poisson"],
            match,
            line_raw=opp.line,
            side=opp.side,
            team_role=team_role,
        )
    if opp.line == "1x2" and "model" in state.data:
        model: ScorelineState = state.data["model"]
        return predict_1x2_probs(model, match).get(opp.side, 0.0)
    return adapter.predict(state, opp, match)


def _model_roi(adapter: Any, state: ModelState, opp: BetOpportunity, match: dict[str, Any], p_model: float) -> float:
    expected_roi = getattr(adapter, "expected_roi", None)
    if expected_roi is not None and "scoreline" in state.data:
        return float(expected_roi(state, opp, match))
    if opp.side in ("over", "under") and "poisson" in state.data:
        team_role = state.data.get("team_role") or team_role_from_market(opp.market)
        return expected_over_under_roi(
            state.data["poisson"],
            match,
            line_raw=opp.line,
            side=opp.side,
            decimal_odds=opp.decimal_odds,
            team_role=team_role,
        )
    return p_model * opp.decimal_odds - 1.0


def _model_fair_odds(adapter: Any, state: ModelState, opp: BetOpportunity, match: dict[str, Any], p_model: float) -> float | None:
    fair_odds = getattr(adapter, "fair_odds", None)
    if fair_odds is not None and "scoreline" in state.data:
        return fair_odds(state, opp, match)
    if opp.side in ("over", "under") and "poisson" in state.data:
        team_role = state.data.get("team_role") or team_role_from_market(opp.market)
        return fair_over_under_odds(
            state.data["poisson"],
            match,
            line_raw=opp.line,
            side=opp.side,
            team_role=team_role,
        )
    return 1.0 / p_model if p_model > 0 else None


def pick_directions(
    *,
    history_matches: list[dict[str, Any]],
    target_matches: list[dict[str, Any]],
    market_keys: list[str],
    min_train_matches: int,
) -> list[dict[str, Any]]:
    adapters = get_adapters(market_keys)
    state_cache: dict[tuple[Any, str], tuple[ModelState | None, int]] = {}
    rows: list[dict[str, Any]] = []
    for match in target_matches:
        target_date = parse_match_date(match["date"])
        train_pool = _history_before(history_matches, target_date)
        for adapter in adapters:
            cache_key = (target_date, adapter.key)
            if cache_key not in state_cache:
                train_matches = adapter.training_matches(train_pool)
                state_cache[cache_key] = (
                    adapter.fit(train_matches) if len(train_matches) >= min_train_matches else None,
                    len(train_matches),
                )
            state, train_size = state_cache[cache_key]
            if state is None:
                continue
            opps = adapter.extract_opportunities(match)
            groups: dict[tuple[str, str], list[BetOpportunity]] = {}
            for opp in opps:
                groups.setdefault(_group_key(opp), []).append(opp)
            for group in groups.values():
                scored = []
                for opp in group:
                    p_model = _model_probability(adapter, state, opp, match)
                    p_market = _market_probability(opp, group)
                    roi = _model_roi(adapter, state, opp, match, p_model)
                    fair = _model_fair_odds(adapter, state, opp, match, p_model)
                    scored.append((p_model, roi, opp, p_market, fair))
                if not scored:
                    continue
                scored.sort(key=lambda item: (-item[0], -item[1], item[2].side))
                p_model, roi, opp, p_market, fair = scored[0]
                rows.append(
                    {
                        "date": target_date.isoformat(),
                        "match_id": opp.match_id,
                        "teams": opp.teams,
                        "market": opp.market,
                        "line": opp.line,
                        "chosen_side": opp.side,
                        "odds": round(opp.decimal_odds, 4),
                        "p_model_old": round(p_model, 4),
                        "p_market": round(p_market, 4) if p_market is not None else "",
                        "prob_edge": round(p_model - p_market, 4) if p_market is not None else "",
                        "model_ev": round(roi, 4),
                        "model_breakeven_odds": round(fair, 4) if fair is not None else "",
                        "support_matches": train_size,
                        "direction_only": "YES",
                        "bet_recommendation": "BET" if roi > 0 else "NO_BET",
                    }
                )
    return sorted(rows, key=lambda r: (r["date"], r["match_id"], r["market"], r["line"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pick higher old-model probability side per match market line")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS_JSON)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--snapshot-date", default=None)
    parser.add_argument("--target-features", type=Path, default=None)
    parser.add_argument("--markets", default=DEFAULT_MARKETS, help=f"Known: {', '.join(list_markets())}")
    parser.add_argument("--min-train-matches", type=int, default=100)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    snapshot = args.snapshot or latest_snapshot(date=args.snapshot_date)
    if snapshot is None:
        print("No snapshot found.", file=sys.stderr)
        return 1
    history = load_matches(args.records)
    targets = load_target_matches(snapshot)
    if args.target_features is not None:
        targets = merge_external_features(targets, load_feature_map(args.target_features))
    rows = pick_directions(
        history_matches=history,
        target_matches=targets,
        market_keys=[m.strip() for m in args.markets.split(",") if m.strip()],
        min_train_matches=args.min_train_matches,
    )
    if not rows:
        print("No direction rows found")
        return 0
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV: {args.out}")
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
