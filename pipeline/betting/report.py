"""Backtest reporting and summary metrics."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.betting.types import BetOutcome, BetRecord


def _roi(records: list[BetRecord], stake: float) -> float:
    if not records:
        return 0.0
    total_staked = len(records) * stake
    if total_staked == 0:
        return 0.0
    return sum(r.pnl for r in records) / total_staked


def _hit_rate(records: list[BetRecord]) -> float:
    if not records:
        return 0.0
    wins = sum(1 for r in records if r.outcome == BetOutcome.WIN)
    return wins / len(records)


def _max_drawdown(pnls: list[float]) -> float:
    peak = 0.0
    cumulative = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def best_per_match(records: list[BetRecord]) -> list[BetRecord]:
    """Keep highest-EV bet per (date, match_id, market)."""
    best: dict[tuple[date, str, str], BetRecord] = {}
    for r in records:
        key = (r.opportunity.date, r.opportunity.match_id, r.opportunity.market)
        if key not in best or r.ev > best[key].ev:
            best[key] = r
    return sorted(best.values(), key=lambda r: (r.opportunity.date, r.opportunity.match_id))


def _group_roi(records: list[BetRecord], stake: float, key_fn) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[BetRecord]] = defaultdict(list)
    for r in records:
        groups[str(key_fn(r))].append(r)
    out: dict[str, dict[str, Any]] = {}
    for k, rs in sorted(groups.items()):
        out[k] = {
            "bets": len(rs),
            "hit_rate": round(_hit_rate(rs), 4),
            "roi": round(_roi(rs, stake), 4),
            "pnl_total": round(sum(r.pnl for r in rs), 2),
        }
    return out


def summarize(records: list[BetRecord], *, stake: float = 1.0) -> dict[str, Any]:
    best = best_per_match(records)

    def _block(rs: list[BetRecord]) -> dict[str, Any]:
        pnls = [r.pnl for r in rs]
        return {
            "bets": len(rs),
            "wins": sum(1 for r in rs if r.outcome == BetOutcome.WIN),
            "hit_rate": round(_hit_rate(rs), 4),
            "roi": round(_roi(rs, stake), 4),
            "pnl_total": round(sum(pnls), 2),
            "max_drawdown": round(_max_drawdown(pnls), 2),
            "by_market": _group_roi(rs, stake, lambda r: r.opportunity.market),
            "by_line": _group_roi(rs, stake, lambda r: r.opportunity.line),
            "by_month": _group_roi(rs, stake, lambda r: r.opportunity.date.strftime("%Y-%m")),
        }

    return {
        "all_bets": _block(records),
        "best_per_match": _block(best),
    }


def records_to_rows(records: list[BetRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in records:
        o = r.opportunity
        rows.append(
            {
                "date": o.date.isoformat(),
                "match_id": o.match_id,
                "teams": o.teams,
                "market": o.market,
                "line": o.line,
                "side": o.side,
                "odds": o.decimal_odds,
                "p_model": round(r.p_model, 4),
                "ev": round(r.ev, 4),
                "p_implied": round(r.p_implied, 4) if r.p_implied is not None else "",
                "edge_vs_close": round(r.edge_vs_close, 4) if r.edge_vs_close is not None else "",
                "result": r.outcome.value,
                "pnl": round(r.pnl, 2),
                "train_size": r.train_size,
            }
        )
    return rows


def write_csv(records: list[BetRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = records_to_rows(records)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_summary(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def print_summary(summary: dict[str, Any]) -> None:
    for label in ("all_bets", "best_per_match"):
        block = summary[label]
        print(f"\n=== {label} ===")
        print(f"bets={block['bets']}  wins={block['wins']}  hit_rate={block['hit_rate']:.2%}")
        print(f"roi={block['roi']:.2%}  pnl_total={block['pnl_total']:.2f}  max_drawdown={block['max_drawdown']:.2f}")
