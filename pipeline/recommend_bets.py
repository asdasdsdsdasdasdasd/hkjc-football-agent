#!/usr/bin/env python3
"""CLI: recommend +EV bets for manually supplied HKJC-style match odds."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from pipeline.betting.load import load_matches
from pipeline.betting.markets import DEFAULT_MARKETS, list_markets
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.recommend import (
    RecommendationConfig,
    load_target_matches,
    recommend_bets,
    recommendations_to_rows,
    write_recommendations_csv,
)
from pipeline.betting.snapshots import latest_snapshot
from pipeline.config import DEFAULT_RECORDS_JSON


def _print_rows(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("PASS: no recommendations met the thresholds")
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HKJC offline +EV bet recommendations")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS_JSON, help="Historical records.json")
    inputs = parser.add_mutually_exclusive_group(required=False)
    inputs.add_argument("--input", type=Path, help="Target match JSON, list, or {matches: [...]} file")
    inputs.add_argument("--snapshot", type=Path, help="Odds snapshot JSON produced by snapshot_hkjc_odds_browser.js")
    parser.add_argument("--snapshot-date", default=None, help="Auto-detect latest snapshot for YYYY-MM-DD")
    parser.add_argument(
        "--markets",
        default=DEFAULT_MARKETS,
        help=f"Comma-separated markets. Known: {', '.join(list_markets())}",
    )
    parser.add_argument("--min-ev", type=float, default=0.05, help="Minimum expected value threshold")
    parser.add_argument("--min-edge", type=float, default=0.0, help="Minimum edge versus vig-free implied probability")
    parser.add_argument("--min-train-matches", type=int, default=200, help="Min training matches before scoring a market")
    parser.add_argument("--best-per-match", action="store_true", help="Keep only the highest-EV bet per match")
    parser.add_argument("--bankroll", type=float, default=None, help="Optional bankroll for stake_amount output")
    parser.add_argument("--kelly-fraction", type=float, default=0.25, help="Fractional Kelly multiplier")
    parser.add_argument("--max-stake-fraction", type=float, default=0.02, help="Max bankroll fraction per bet")
    parser.add_argument("--history-features", type=Path, default=None, help="Optional JSON feature file for historical records")
    parser.add_argument("--target-features", type=Path, default=None, help="Optional JSON feature file for target/snapshot matches")
    parser.add_argument("--out", type=Path, default=None, help="Optional CSV output path")
    args = parser.parse_args(argv)

    input_path = args.input or args.snapshot
    if input_path is None:
        input_path = latest_snapshot(date=args.snapshot_date)
        if input_path is None:
            hint = f" for {args.snapshot_date}" if args.snapshot_date else ""
            print(f"No odds snapshot found{hint}. Run pipeline/snapshot_hkjc_odds_browser.js first.", file=sys.stderr)
            return 1
        print(f"Using latest snapshot: {input_path}", file=sys.stderr)
    if not args.records.exists():
        print(f"Records not found: {args.records}", file=sys.stderr)
        return 1
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 1

    market_keys = [k.strip() for k in args.markets.split(",") if k.strip()]
    history_matches = load_matches(args.records)
    target_matches = load_target_matches(input_path)
    if args.history_features is not None:
        history_matches = merge_external_features(history_matches, load_feature_map(args.history_features))
    if args.target_features is not None:
        target_matches = merge_external_features(target_matches, load_feature_map(args.target_features))
    config = RecommendationConfig(
        market_keys=market_keys,
        min_ev=args.min_ev,
        min_edge=args.min_edge,
        min_train_matches=args.min_train_matches,
        best_per_match=args.best_per_match,
        bankroll=args.bankroll,
        kelly_fraction=args.kelly_fraction,
        max_stake_fraction=args.max_stake_fraction,
    )

    records = recommend_bets(history_matches=history_matches, target_matches=target_matches, config=config)
    rows = recommendations_to_rows(records)
    _print_rows(rows)

    if args.out is not None:
        write_recommendations_csv(records, args.out)
        print(f"\nCSV: {args.out.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
