"""Analyze odds movement from odds-history shards.

For each (match, market, line, side) compute opening -> current odds,
implied-probability shift, and flag steam moves (fast, large shifts).

Usage:
  python3 -m pipeline.odds_moves [--date 2026-08-29] [--min-move 0.03]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "odds-history"


def load_day(iso: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    p = OUT_DIR / f"odds-{iso}.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    meta: dict[str, Any] = {}
    mp = OUT_DIR / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    return rows, meta


def summarize(iso: str) -> list[dict[str, Any]]:
    rows, meta = load_day(iso)
    series: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (r["match_id"], r["market"], str(r["line"]), r["side"])
        series[key].append(r)

    out: list[dict[str, Any]] = []
    for (mid, market, line, side), pts in series.items():
        pts.sort(key=lambda r: r["ts"])
        first, last = pts[0], pts[-1]
        o0, o1 = first["odds"], last["odds"]
        if not o0 or not o1:
            continue
        p0, p1 = 1.0 / o0, 1.0 / o1
        move = p1 - p0  # positive = odds shortened = money on this side
        # steam: largest single jump
        max_jump = 0.0
        for a, b in zip(pts, pts[1:]):
            if a["odds"] and b["odds"]:
                max_jump = max(max_jump, abs(1.0 / b["odds"] - 1.0 / a["odds"]))
        m = meta.get(mid) or {}
        out.append({
            "match_id": mid,
            "match": f"{m.get('home', '?')} vs {m.get('away', '?')}",
            "league": m.get("league"),
            "kickoff": m.get("kickoff"),
            "market": market,
            "line": line,
            "side": side,
            "main": last.get("main"),
            "open": o0,
            "now": o1,
            "move_pp": round(move * 100, 2),  # percentage points of implied prob
            "max_jump_pp": round(max_jump * 100, 2),
            "n_changes": len(pts),
            "first_ts": first["ts"],
            "last_ts": last["ts"],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--min-move", type=float, default=2.0, help="min abs move in pp")
    ap.add_argument("--market", default=None)
    args = ap.parse_args()

    rows = summarize(args.date)
    rows = [r for r in rows if abs(r["move_pp"]) >= args.min_move]
    if args.market:
        rows = [r for r in rows if r["market"] == args.market]
    rows.sort(key=lambda r: -abs(r["move_pp"]))

    print(f"=== odds moves {args.date} (|move| >= {args.min_move}pp) — {len(rows)} series")
    for r in rows[:60]:
        direction = "SHORTEN" if r["move_pp"] > 0 else "drift"
        print(f"{r['match_id']} {r['match'][:28]:28} {r['market']:11} [{r['line']}] {r['side']} "
              f"{r['open']:.2f}->{r['now']:.2f} {direction} {r['move_pp']:+.1f}pp "
              f"jump={r['max_jump_pp']:.1f}pp n={r['n_changes']}")


if __name__ == "__main__":
    main()
