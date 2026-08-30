"""Keep-alive football desk: odds + intel + LLM card, in one process.

  python3 -m pipeline.monitor
  python3 -m pipeline.monitor --once

Odds poll every --odds-interval seconds.
News/lineup/injury refresh every --intel-interval seconds.
LLM card rebuild every --card-interval seconds (skipped if nothing changed).

Leave this running. Start script: bin/start-desk.sh
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import os

from pipeline import bet_card, llm, match_intel, odds_tracker

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
ALERTS = OUT / "alerts.jsonl"
PID = ROOT / "odds-history" / "monitor.pid"
HKT = timezone(timedelta(hours=8))


def now_hkt() -> datetime:
    return datetime.now(HKT)


def find_books() -> list[Path]:
    today = now_hkt().date()
    paths: list[Path] = []
    for d in (today, today + timedelta(days=1)):
        p = OUT / f"tomorrow_{d.strftime('%Y%m%d')}_v32_live.json"
        if p.exists():
            paths.append(p)
    return paths


def book_ids(paths: list[Path]) -> list[str]:
    ids: list[str] = []
    for p in paths:
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for r in rows:
            mid = r.get("match_id")
            if mid and mid not in ids:
                ids.append(mid)
    return ids


def log_alert(kind: str, payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    row = {"ts": now_hkt().isoformat(timespec="seconds"), "kind": kind, **payload}
    with ALERTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    extra = payload.get("match_id", "")
    print(f"[alert] {kind} {extra} {payload.get('detail', '')}", flush=True)


def steam_from_changes(changes: list[dict[str, Any]], watch: set[str], min_pp: float) -> None:
    for c in changes:
        if c.get("match_id") not in watch:
            continue
        if c.get("prev_odds") is None:
            continue
        try:
            o0, o1 = float(c["prev_odds"]), float(c["odds"])
            move_pp = (1.0 / o1 - 1.0 / o0) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if abs(move_pp) < min_pp:
            continue
        log_alert("steam", {
            "match_id": c["match_id"],
            "market": c.get("market"),
            "line": c.get("line"),
            "side": c.get("side"),
            "from": o0,
            "to": o1,
            "move_pp": round(move_pp, 2),
            "detail": f"{c.get('market')} [{c.get('line')}] {c.get('side')} {o0}->{o1} {move_pp:+.1f}pp",
        })


def refresh_intel(ids: list[str]) -> Path | None:
    if not ids:
        return None
    payload = match_intel.collect(ids)
    match_intel.INTEL_DIR.mkdir(parents=True, exist_ok=True)
    out = match_intel.INTEL_DIR / f"intel-{now_hkt().strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n_fm = sum(1 for m in payload["matches"].values() if m.get("fotmob"))
    print(f"[intel] {payload['n']} matches, fotmob={n_fm} -> {out}", flush=True)
    return out


def refresh_card(book_path: Path, intel_path: Path | None) -> Path | None:
    book = json.loads(book_path.read_text(encoding="utf-8"))
    iso = None
    for r in book:
        if r.get("date"):
            iso = str(r["date"])
            break
    iso = iso or now_hkt().date().isoformat()
    intel_map = bet_card.load_intel_map(intel_path)
    card = bet_card.build_card(book, move_date=now_hkt().date().isoformat(), use_llm=llm.health(),
                               intel_map=intel_map)
    out = OUT / f"card_{iso.replace('-', '')}.json"
    out.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    bets = [e for e in card if (e.get("llm") or {}).get("verdict") == "bet"]
    print(f"[card] {len(card)} rows, llm-bet={len(bets)} -> {out}", flush=True)
    for e in card:
        v = (e.get("llm") or {}).get("verdict", "-")
        print(f"  {e.get('match_id')} {e.get('pick')} ev={e.get('ev')} llm={v}", flush=True)
    return out


def write_pid() -> None:
    PID.parent.mkdir(parents=True, exist_ok=True)
    PID.write_text(str(os.getpid()), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--odds-interval", type=float, default=120.0)
    ap.add_argument("--intel-interval", type=float, default=1800.0)
    ap.add_argument("--card-interval", type=float, default=1800.0)
    ap.add_argument("--steam-pp", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    write_pid()
    state = odds_tracker.load_state()
    last_intel = 0.0
    last_card = 0.0
    intel_path: Path | None = None
    print(f"[monitor] start llama={'ok' if llm.health() else 'DOWN'} odds_state={len(state)}", flush=True)

    while True:
        t0 = time.time()
        books = find_books()
        watch = set(book_ids(books))
        try:
            seen, changed, change_rows = odds_tracker.poll_once(state)
            print(f"[odds] {now_hkt().isoformat(timespec='seconds')} rows={seen} changes={changed}", flush=True)
            steam_from_changes(change_rows, watch, args.steam_pp)
        except Exception as e:  # noqa: BLE001
            print(f"[odds] ERROR {e}", flush=True)

        now = time.time()
        if watch and (now - last_intel >= args.intel_interval or last_intel == 0):
            try:
                intel_path = refresh_intel(sorted(watch))
                last_intel = now
            except Exception as e:  # noqa: BLE001
                print(f"[intel] ERROR {e}", flush=True)

        if books and (now - last_card >= args.card_interval or last_card == 0):
            try:
                refresh_card(books[-1], intel_path)
                last_card = now
            except Exception as e:  # noqa: BLE001
                print(f"[card] ERROR {e}", flush=True)

        if args.once:
            return 0
        dt = time.time() - t0
        time.sleep(max(5.0, args.odds_interval - dt))


if __name__ == "__main__":
    raise SystemExit(main())
