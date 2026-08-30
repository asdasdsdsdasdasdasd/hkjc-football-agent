"""HKJC football odds tracker daemon.

Polls the GraphQL odds board every N seconds and appends CHANGED odds rows
to odds-history/odds-YYYY-MM-DD.jsonl (one JSON object per line):
  {ts, match_id, market, line, side, odds, main, prev_odds}
Match metadata (teams/league/kickoff) goes to odds-history/meta.json each poll.

Usage:
  python3 -m pipeline.odds_tracker --interval 120
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from pipeline.odds_api import fetch_all, iter_odds_rows

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "odds-history"
STATE_PATH = OUT_DIR / "state.json"
HKT = timezone(timedelta(hours=8))

MARKET_NAMES = {
    "HAD": "1x2", "EHA": "1x2_es",
    "HIL": "goal_ou", "EHL": "goal_ou_es",
    "FHL": "goal_ou_ht",
    "CHL": "corner_ou", "ECH": "corner_ou_es",
    "FCH": "corner_ou_ht",
}


def now_hkt() -> datetime:
    return datetime.now(HKT)


def shard_path(ts: datetime) -> Path:
    return OUT_DIR / f"odds-{ts.date().isoformat()}.jsonl"


def _key_str(key: tuple) -> str:
    return "\t".join(str(x) for x in key)


def load_state() -> dict[tuple, float]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[tuple, float] = {}
    for k, v in raw.items():
        parts = tuple(k.split("\t"))
        try:
            out[parts] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def save_state(state: dict[tuple, float]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({_key_str(k): v for k, v in state.items()}, ensure_ascii=False),
        encoding="utf-8",
    )


def poll_once(state: dict[tuple, float]) -> tuple[int, int, list[dict[str, Any]]]:
    """One poll. Returns (rows_seen, rows_changed, change_rows)."""
    ts = now_hkt()
    by_id = fetch_all()
    meta: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []
    seen = 0
    seeding = len(state) == 0
    for fid, m in by_id.items():
        meta[fid] = {
            "home": (m.get("homeTeam") or {}).get("name_ch"),
            "away": (m.get("awayTeam") or {}).get("name_ch"),
            "home_en": (m.get("homeTeam") or {}).get("name_en"),
            "away_en": (m.get("awayTeam") or {}).get("name_en"),
            "league": (m.get("tournament") or {}).get("name_ch"),
            "league_en": (m.get("tournament") or {}).get("name_en"),
            "kickoff": m.get("kickOffTime"),
            "status": m.get("status"),
        }
        for r in iter_odds_rows(m):
            seen += 1
            key = (r["match_id"], r["market"], str(r["line"]), r["side"])
            prev = state.get(key)
            if prev is None or abs(prev - r["odds"]) > 1e-9:
                state[key] = r["odds"]
                if seeding and prev is None:
                    continue
                changes.append({
                    "ts": ts.isoformat(timespec="seconds"),
                    "match_id": r["match_id"],
                    "market": MARKET_NAMES.get(r["market"], r["market"]),
                    "line": r["line"],
                    "side": r["side"],
                    "odds": r["odds"],
                    "prev_odds": prev,
                    "main": r["main"],
                })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if changes:
        with shard_path(ts).open("a", encoding="utf-8") as f:
            for c in changes:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    save_state(state)
    return seen, len(changes), changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=120.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    state: dict[tuple, float] = load_state()
    print(f"[odds-tracker] start interval={args.interval}s out={OUT_DIR} state={len(state)}", flush=True)
    while True:
        t0 = time.time()
        try:
            seen, changed, _ = poll_once(state)
            print(f"[odds-tracker] {now_hkt().isoformat(timespec='seconds')} rows={seen} changes={changed}", flush=True)
        except Exception as e:  # noqa: BLE001 - daemon must not die
            print(f"[odds-tracker] ERROR {e}", flush=True)
        if args.once:
            return
        dt = time.time() - t0
        time.sleep(max(5.0, args.interval - dt))


if __name__ == "__main__":
    main()
