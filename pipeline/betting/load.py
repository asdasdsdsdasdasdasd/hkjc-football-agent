"""Load and normalize match records from records.json."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pipeline.config import DEFAULT_RECORDS_JSON


def parse_match_date(value: str) -> date:
    """Parse DD/MM/YYYY match date."""
    return datetime.strptime(value.strip(), "%d/%m/%Y").date()


def load_matches(path: Path = DEFAULT_RECORDS_JSON) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)
    matches: list[dict[str, Any]] = payload.get("matches") or []
    matches.sort(key=lambda m: (parse_match_date(m["date"]), m.get("match_id", "")))
    return matches


def split_teams(teams: str) -> tuple[str, str] | None:
    """Return (home, away) from 'A 對 B'."""
    if not teams:
        return None
    parts = teams.split(" 對 ")
    if len(parts) != 2:
        return None
    home, away = parts[0].strip(), parts[1].strip()
    if not home or not away:
        return None
    return home, away


def parse_score_total(score: str | None) -> int | None:
    """Parse '1 : 5' into total goals."""
    if not score:
        return None
    parts = score.replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        home, away = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return home + away
