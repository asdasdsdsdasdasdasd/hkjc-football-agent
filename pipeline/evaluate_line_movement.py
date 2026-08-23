#!/usr/bin/env python3
"""CLI: compare odds movement between two HKJC odds snapshots."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from pipeline.betting.clv import compare_clv
from pipeline.betting.markets import DEFAULT_MARKETS, list_markets
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.snapshots import latest_two_snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare line movement between two odds snapshots")
    parser.add_argument("--from", dest="from_path", type=Path, default=None, help="Earlier odds snapshot")
    parser.add_argument("--to", dest="to_path", type=Path, default=None, help="Later odds snapshot")
    parser.add_argument("--snapshot-date", default=None, help="Auto-detect latest two snapshots for YYYY-MM-DD")
    parser.add_argument(
        "--markets",
        default=DEFAULT_MARKETS,
        help=f"Comma-separated markets. Known: {', '.join(list_markets())}",
    )
    parser.add_argument("--out", type=Path, default=None, help="Optional CSV output path")
    args = parser.parse_args()

    from_path = args.from_path
    to_path = args.to_path
    if from_path is None or to_path is None:
        pair = latest_two_snapshots(date=args.snapshot_date)
        if pair is None:
            hint = f" for {args.snapshot_date}" if args.snapshot_date else ""
            print(f"Need at least two odds snapshots{hint}. Run snapshot_hkjc_odds_browser.js more than once.")
            return 1
        from_path, to_path = pair
        print(f"Using snapshots: {from_path} -> {to_path}")

    market_keys = [m.strip() for m in args.markets.split(",") if m.strip()]
    records = compare_clv(
        entry_matches=load_target_matches(from_path),
        closing_matches=load_target_matches(to_path),
        market_keys=market_keys,
    )
    rows = []
    for r in records:
        row = r.to_row()
        row["odds_delta"] = round(r.closing_odds - r.entry_odds, 4)
        row["movement"] = "better" if r.clv > 0 else ("worse" if r.clv < 0 else "flat")
        rows.append(row)

    if not rows:
        print("No comparable odds rows found")
        return 0

    positive = sum(1 for r in records if r.clv > 0)
    avg_clv = sum(r.clv for r in records) / len(records)
    print(f"rows={len(records)} positive_clv={positive} positive_rate={positive/len(records):.2%} avg_clv={avg_clv:.4%}")

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
