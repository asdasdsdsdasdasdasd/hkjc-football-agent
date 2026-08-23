#!/usr/bin/env python3
"""CLI: compare entry odds against closing/current odds snapshots."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from pipeline.betting.clv import compare_clv
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.markets import DEFAULT_MARKETS, list_markets


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate closing-line value between two odds snapshots")
    parser.add_argument("--entry", type=Path, required=True, help="Entry/opening odds JSON snapshot")
    parser.add_argument("--closing", type=Path, required=True, help="Closing/current odds JSON snapshot")
    parser.add_argument(
        "--markets",
        default=DEFAULT_MARKETS,
        help=f"Comma-separated markets. Known: {', '.join(list_markets())}",
    )
    parser.add_argument("--out", type=Path, default=None, help="Optional CSV output path")
    args = parser.parse_args()

    market_keys = [m.strip() for m in args.markets.split(",") if m.strip()]
    rows = [
        r.to_row()
        for r in compare_clv(
            entry_matches=load_target_matches(args.entry),
            closing_matches=load_target_matches(args.closing),
            market_keys=market_keys,
        )
    ]
    if not rows:
        print("No comparable odds rows found")
        return 0

    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as f:
            out = csv.DictWriter(f, fieldnames=fieldnames)
            out.writeheader()
            out.writerows(rows)
        print(f"\nCSV: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
