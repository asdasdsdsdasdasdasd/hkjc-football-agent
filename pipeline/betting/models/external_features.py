"""Optional external match features for result prediction.

The model must not invent injury, xG, lineup, weather, or team-news values.
This module only normalizes fields supplied by an external data file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FEATURE_KEYS = {
    "home_xg",
    "away_xg",
    "home_shots",
    "away_shots",
    "home_injuries",
    "away_injuries",
    "home_lineup_strength",
    "away_lineup_strength",
    "home_fatigue",
    "away_fatigue",
    "weather_goal_factor",
    "team_news_edge",
}


def match_feature_key(match: dict[str, Any]) -> str:
    match_id = str(match.get("match_id") or "").strip()
    if match_id:
        return match_id
    return f"{match.get('date', '')}|{match.get('teams', '')}"


def numeric_features(raw: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in FEATURE_KEYS:
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def load_feature_map(path: Path) -> dict[str, dict[str, float]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("matches"), list):
        rows = payload["matches"]
    elif isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        raise ValueError(f"Unsupported feature file shape: {path}")

    out: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("match_id") or "").strip()
        if not key:
            key = f"{row.get('date', '')}|{row.get('teams', '')}"
        features = numeric_features(row)
        if key and features:
            out[key] = features
    return out


def merge_external_features(matches: list[dict[str, Any]], feature_map: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in matches:
        copied = dict(match)
        features = feature_map.get(match_feature_key(match))
        if features:
            existing = copied.get("external_features") if isinstance(copied.get("external_features"), dict) else {}
            copied["external_features"] = {**existing, **features}
        out.append(copied)
    return out


def feature_edge(features: dict[str, Any], home_key: str, away_key: str, *, cap: float) -> float:
    try:
        home = float(features.get(home_key, 0.0))
        away = float(features.get(away_key, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(-cap, min(cap, home - away))
