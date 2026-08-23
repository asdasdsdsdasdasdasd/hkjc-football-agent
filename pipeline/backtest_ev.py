#!/usr/bin/env python3
"""CLI: walk-forward +EV backtest on HKJC records.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.betting.backtest import BacktestConfig, run_backtest
from pipeline.betting.load import load_matches
from pipeline.betting.markets import list_markets
from pipeline.betting.report import print_summary, summarize, write_csv, write_summary
from pipeline.config import DEFAULT_RECORDS_JSON, OUTPUT_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HKJC +EV walk-forward backtest")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS_JSON, help="Input records.json")
    parser.add_argument(
        "--markets",
        default="corner_ou_ft",
        help=f"Comma-separated markets. Known: {', '.join(list_markets())}",
    )
    parser.add_argument("--min-ev", type=float, default=0.05, help="Minimum EV threshold")
    parser.add_argument("--min-train-matches", type=int, default=200, help="Min training matches before betting")
    parser.add_argument("--stake", type=float, default=1.0, help="Flat stake per bet")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR / "backtest_ev.csv", help="CSV detail output")
    parser.add_argument(
        "--summary",
        type=Path,
        default=OUTPUT_DIR / "backtest_summary.json",
        help="JSON summary output",
    )
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV output")
    parser.add_argument("--quiet", action="store_true", help="Suppress console summary")
    args = parser.parse_args(argv)

    if not args.records.exists():
        print(f"Records not found: {args.records}", file=sys.stderr)
        return 1

    market_keys = [k.strip() for k in args.markets.split(",") if k.strip()]
    matches = load_matches(args.records)
    config = BacktestConfig(
        market_keys=market_keys,
        min_ev=args.min_ev,
        min_train_matches=args.min_train_matches,
        stake=args.stake,
    )

    records = run_backtest(matches, config)
    summary = summarize(records, stake=args.stake)

    if not args.no_csv:
        write_csv(records, args.out)
    write_summary(summary, args.summary)

    if not args.quiet:
        print(f"Loaded {len(matches)} matches from {args.records}")
        print(f"Markets: {', '.join(market_keys)}  min_ev={args.min_ev}  min_train={args.min_train_matches}")
        print_summary(summary)
        if not args.no_csv:
            print(f"\nCSV: {args.out.resolve()}")
        print(f"Summary: {args.summary.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
