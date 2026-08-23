#!/usr/bin/env python3
"""Annotate forward bets with conservative empirical confidence metrics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.betting.confidence import build_profile


def _read_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [row for row in payload if isinstance(row, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add empirical confidence fields to forward bets")
    parser.add_argument("--bets", type=Path, required=True, help="Unsettled or forward bet JSON list")
    parser.add_argument(
        "--history",
        type=Path,
        nargs="+",
        required=True,
        help="Settled bet-result JSON lists that predate --bets",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, default=None)
    args = parser.parse_args(argv)

    bets = _read_rows(args.bets)
    history = [row for path in args.history for row in _read_rows(path)]
    profile = build_profile(history)
    annotated = [{**bet, **profile.annotate(bet)} for bet in bets]
    summary = {
        "source_bets": str(args.bets),
        "history_files": [str(path) for path in args.history],
        "settled_history_n": len(profile.settled_rows),
        "overall_history_win_rate": profile.overall["win_rate"],
        "bets": len(annotated),
        "confidence_labels": {
            label: sum(1 for bet in annotated if bet["confidence_label"] == label)
            for label in ("HIGH", "MEDIUM", "UNPROVEN")
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(annotated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = args.summary_out or args.out.with_name(f"{args.out.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
