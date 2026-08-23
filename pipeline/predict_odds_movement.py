#!/usr/bin/env python3
"""Predict near-term odds shortening from saved HKJC browser snapshots."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from pipeline.betting.markets import DEFAULT_MARKET_KEYS
from pipeline.betting.odds_movement import current_movement_signals, load_snapshot_points, movement_training_rows
from pipeline.betting.snapshots import SNAPSHOT_DIR, list_snapshots


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score odds-movement buy signals from HKJC snapshots")
    parser.add_argument("--snapshot-date", default=None, help="Use snapshots whose date_range starts at YYYY-MM-DD")
    parser.add_argument("--snapshot-dir", type=Path, default=SNAPSHOT_DIR, help="Directory containing odds snapshot JSON files")
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_MARKET_KEYS),
        help=f"Comma-separated markets. Known: {', '.join(list_markets())}",
    )
    parser.add_argument("--horizon-minutes", type=int, default=30, help="Training label horizon for dataset output")
    parser.add_argument("--threshold", type=float, default=0.60, help="Minimum shorten_score to print as a buy signal")
    parser.add_argument("--limit", type=int, default=25, help="Max signals to print")
    parser.add_argument("--out", type=Path, default=None, help="Optional CSV output for current signals")
    parser.add_argument("--dataset-out", type=Path, default=None, help="Optional CSV output for supervised movement rows")
    args = parser.parse_args(argv)

    paths = list_snapshots(date=args.snapshot_date, directory=args.snapshot_dir)
    if len(paths) < 2:
        hint = f" for {args.snapshot_date}" if args.snapshot_date else ""
        print(f"Need at least two snapshots{hint}.", file=sys.stderr)
        return 1

    market_keys = [m.strip() for m in args.markets.split(",") if m.strip()]
    points = load_snapshot_points(paths, market_keys=market_keys)
    signals = current_movement_signals(points)
    rows = [s.to_row() for s in signals]

    if args.out is not None:
        _write_rows(args.out, rows)

    if args.dataset_out is not None:
        _write_rows(args.dataset_out, movement_training_rows(points, horizon_minutes=args.horizon_minutes))

    picks = [s for s in signals if s.shorten_score >= args.threshold]
    print(
        f"snapshots={len(paths)} opportunities={len(signals)} "
        f"signals>={args.threshold:.2f}={len(picks)}"
    )
    if not picks:
        print("NO_BUY: no odds-movement signal strong enough")
        return 0

    writer = csv.DictWriter(sys.stdout, fieldnames=list(picks[0].to_row().keys()))
    writer.writeheader()
    for signal in picks[: args.limit]:
        writer.writerow(signal.to_row())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
