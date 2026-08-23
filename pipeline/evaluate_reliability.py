#!/usr/bin/env python3
"""CLI: reliability curves + raw/market/calibrated v5 comparison for goal totals."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pipeline.betting.backtest import BacktestConfig, run_backtest, run_goal_model_comparison
from pipeline.betting.load import load_matches
from pipeline.betting.markets import DEFAULT_MARKETS
from pipeline.betting.types import BetOutcome, BetRecord
from pipeline.config import DEFAULT_RECORDS_JSON, OUTPUT_DIR


def _actual(record: BetRecord) -> float | None:
    if record.outcome == BetOutcome.WIN:
        return 1.0
    if record.outcome == BetOutcome.LOSE:
        return 0.0
    return None


def reliability(records: list[BetRecord], *, bins: int = 10) -> dict[str, Any]:
    usable = [(r.p_model, _actual(r)) for r in records if _actual(r) is not None]
    if not usable:
        return {"n": 0, "ece": 0.0, "brier": 0.0, "buckets": []}

    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for p, actual in usable:
        assert actual is not None
        idx = min(bins - 1, max(0, int(p * bins)))
        buckets[idx].append((p, actual))

    out = []
    ece = 0.0
    brier = 0.0
    n = len(usable)
    for idx, vals in enumerate(buckets):
        if not vals:
            continue
        avg_pred = sum(p for p, _ in vals) / len(vals)
        win_rate = sum(a for _, a in vals) / len(vals)
        ece += (len(vals) / n) * abs(avg_pred - win_rate)
        brier += sum((p - a) ** 2 for p, a in vals)
        out.append(
            {
                "bucket": idx,
                "range": [round(idx / bins, 4), round((idx + 1) / bins, 4)],
                "n": len(vals),
                "avg_pred": round(avg_pred, 4),
                "actual_win_rate": round(win_rate, 4),
                "gap": round(avg_pred - win_rate, 4),
            }
        )

    return {
        "n": n,
        "ece": round(ece, 4),
        "brier": round(brier / n, 4),
        "buckets": out,
    }


def grouped_reliability(records: list[BetRecord], *, bins: int = 10, min_group: int = 100) -> dict[str, Any]:
    groups: dict[str, list[BetRecord]] = defaultdict(list)
    for r in records:
        groups[f"market:{r.opportunity.market}"].append(r)
        groups[f"competition:{r.opportunity.competition or 'unknown'}"].append(r)
        groups[f"market_competition:{r.opportunity.market}:{r.opportunity.competition or 'unknown'}"].append(r)

    return {
        key: reliability(vals, bins=bins)
        for key, vals in sorted(groups.items())
        if len(vals) >= min_group
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate prediction calibration with reliability buckets")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS_JSON)
    parser.add_argument(
        "--markets",
        default=DEFAULT_MARKETS,
    )
    parser.add_argument("--min-train-matches", type=int, default=200)
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument("--min-group", type=int, default=100)
    parser.add_argument("--compare-goals", action="store_true", help="Also report raw/market/v5 goal comparison")
    parser.add_argument("--compare-min-ev", type=float, default=0.0)
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR / "reliability_summary.json")
    args = parser.parse_args()

    market_keys = [m.strip() for m in args.markets.split(",") if m.strip()]
    matches = load_matches(args.records)
    records = run_backtest(
        matches,
        BacktestConfig(market_keys=market_keys, min_ev=-999.0, min_train_matches=args.min_train_matches, stake=1.0),
    )
    payload: dict[str, Any] = {
        "markets": market_keys,
        "min_train_matches": args.min_train_matches,
        "overall": reliability(records, bins=args.bins),
        "groups": grouped_reliability(records, bins=args.bins, min_group=args.min_group),
    }
    if args.compare_goals:
        payload["goal_model_comparison"] = run_goal_model_comparison(
            matches,
            market_keys=["goal_ou_ft", "goal_ou_ht"],
            min_train_matches=min(100, args.min_train_matches),
            min_ev=args.compare_min_ev,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["overall"], ensure_ascii=False, indent=2))
    if "goal_model_comparison" in payload:
        print(json.dumps(payload["goal_model_comparison"], ensure_ascii=False, indent=2))
    print(f"Summary: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
