"""Backfill missing competition on snapshot/target matches from records or DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.betting.load import parse_match_date
from pipeline.config import DEFAULT_DB, DEFAULT_RECORDS_JSON, OUTPUT_DIR


def record_lookup_key(date_value: str, match_id: str) -> tuple[str, str]:
    """Normalize to (DD/MM/YYYY, match_id) for recycled HKJC IDs."""
    return (normalize_match_date(date_value), str(match_id or "").strip())


def normalize_match_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        y, m, d = text.split("-")
        return parse_match_date(f"{d}/{m}/{y}").strftime("%d/%m/%Y")
    if len(text) == 10 and text[2] == "/" and text[5] == "/":
        return parse_match_date(text).strftime("%d/%m/%Y")
    return text


def _competition_value(rec: dict[str, Any]) -> str:
    return str(rec.get("competition") or "").strip()


@lru_cache(maxsize=4)
def build_competition_lookup(
    records_path: str,
    db_path: str,
    jsonl_glob: str,
) -> dict[tuple[str, str], str]:
    """Build (date, match_id) -> competition from records.json, JSONL shards, then DB."""
    lookup: dict[tuple[str, str], str] = {}
    records_file = Path(records_path)
    if records_file.exists():
        payload = json.loads(records_file.read_text(encoding="utf-8"))
        matches = payload.get("matches") if isinstance(payload, dict) else payload
        if isinstance(matches, list):
            for rec in matches:
                if not isinstance(rec, dict):
                    continue
                comp = _competition_value(rec)
                if not comp:
                    continue
                key = record_lookup_key(rec.get("date", ""), rec.get("match_id", ""))
                if key[1]:
                    lookup[key] = comp

    for path in sorted(OUTPUT_DIR.glob(jsonl_glob)):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            comp = _competition_value(rec)
            if not comp:
                continue
            key = record_lookup_key(rec.get("date", ""), rec.get("match_id", ""))
            if key[1]:
                lookup[key] = comp

    db_file = Path(db_path)
    if db_file.exists():
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT match_date, match_id, competition
                FROM matches
                WHERE competition IS NOT NULL AND TRIM(competition) != ''
                """
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            comp = str(row["competition"] or "").strip()
            if not comp:
                continue
            key = record_lookup_key(row["match_date"] or "", row["match_id"] or "")
            if key[1] and key not in lookup:
                lookup[key] = comp

    return lookup


def enrich_match_competitions(
    matches: list[dict[str, Any]],
    *,
    records_path: Path = DEFAULT_RECORDS_JSON,
    db_path: Path = DEFAULT_DB,
    jsonl_glob: str = "records-*.jsonl",
    lookup: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """Return copies with blank competition filled from records/DB when possible."""
    if lookup is None:
        lookup = build_competition_lookup(str(records_path), str(db_path), jsonl_glob)

    enriched: list[dict[str, Any]] = []
    for match in matches:
        copied = dict(match)
        if _competition_value(copied):
            enriched.append(copied)
            continue
        key = record_lookup_key(copied.get("date", ""), copied.get("match_id", ""))
        comp = lookup.get(key)
        if comp:
            copied["competition"] = comp
        enriched.append(copied)
    return enriched
