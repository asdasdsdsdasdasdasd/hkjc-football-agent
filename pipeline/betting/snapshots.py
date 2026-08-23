"""Locate saved HKJC odds snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.config import OUTPUT_DIR

SNAPSHOT_DIR = OUTPUT_DIR / "odds_snapshots"


def _snapshot_date(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as f:
            payload: dict[str, Any] = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    date_range = payload.get("date_range")
    return str(date_range).split("..", 1)[0] if date_range else None


def list_snapshots(*, date: str | None = None, directory: Path = SNAPSHOT_DIR) -> list[Path]:
    if not directory.exists():
        return []
    paths = [p for p in directory.glob("*.json") if p.is_file()]
    if date is not None:
        paths = [p for p in paths if _snapshot_date(p) == date or date in p.name]
    return sorted(paths, key=lambda p: (p.stat().st_mtime, p.name))


def latest_snapshot(*, date: str | None = None, directory: Path = SNAPSHOT_DIR) -> Path | None:
    paths = list_snapshots(date=date, directory=directory)
    return paths[-1] if paths else None


def latest_two_snapshots(*, date: str | None = None, directory: Path = SNAPSHOT_DIR) -> tuple[Path, Path] | None:
    paths = list_snapshots(date=date, directory=directory)
    if len(paths) < 2:
        return None
    return paths[-2], paths[-1]
