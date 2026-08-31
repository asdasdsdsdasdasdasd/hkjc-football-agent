#!/usr/bin/env python3
"""Dump scored HKJC matches from monthly discover SQLite shards into records.json.

Unique on (match_id, date) because FB ids recycle across seasons.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARDS = ROOT / "data" / "shards"
OUT = ROOT / "output" / "records.json"


def iso_to_ddmmyyyy(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return d.strftime("%d/%m/%Y")


def main() -> int:
    rows: dict[tuple[str, str], dict] = {}
    dbs = sorted(SHARDS.glob("*.db"))
    if not dbs:
        print("no shard dbs found", file=sys.stderr)
        return 2
    for db_path in dbs:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        cur = con.execute(
            """
            SELECT match_id, match_date, competition, teams, ht_score, ft_score
            FROM matches
            WHERE ft_score IS NOT NULL AND TRIM(ft_score) != ''
            """
        )
        n = 0
        for r in cur:
            iso = str(r["match_date"] or "")[:10]
            if len(iso) < 10:
                continue
            key = (r["match_id"], iso)
            rec = {
                "date": iso_to_ddmmyyyy(iso),
                "match_id": r["match_id"],
                "competition": r["competition"] or "",
                "teams": r["teams"] or "",
                "scores": {
                    "half_time": r["ht_score"] or "",
                    "full_time": r["ft_score"] or "",
                },
            }
            rows[key] = rec
            n += 1
        con.close()
        print(f"{db_path.name}: {n} scored rows")

    matches = sorted(rows.values(), key=lambda m: (datetime.strptime(m["date"], "%d/%m/%Y").date(), m["match_id"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "discover-scores",
        "unique_on": "match_id+date",
        "match_count": len(matches),
        "matches": matches,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(matches)} unique scored matches -> {OUT}")
    if matches:
        print(f"date range {matches[0]['date']} -> {matches[-1]['date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
