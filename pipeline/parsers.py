"""Pure parsers for HKJC corner counts and closing odds."""

from __future__ import annotations

import re
from typing import Any

from pipeline.config import EXCLUDE_ODDS_HEADERS, ODDS_SECTIONS

TEAM_OU_SECTIONS = frozenset(
    {
        "球隊入球大細",
        "球隊半場入球大細",
        "球隊開出角球大細",
        "球隊半場開出角球大細",
    }
)
TEAM_LABELS_HOME = frozenset({"主隊", "主", "Home"})
TEAM_LABELS_AWAY = frozenset({"客隊", "客", "Away"})

CORNER_PATTERNS = (
    (r"半場開出角球\s*(\d+)\s*\(\s*(\d+)\s*:\s*(\d+)\s*\)", "half_time"),
    (r"全場開出角球\s*(\d+)\s*\(\s*(\d+)\s*:\s*(\d+)\s*\)", "full_time"),
    (r"半場角球\s*(\d+)\s*\(\s*(\d+)\s*:\s*(\d+)\s*\)", "half_time"),
    (r"全場角球\s*(\d+)\s*\(\s*(\d+)\s*:\s*(\d+)\s*\)", "full_time"),
)


def parse_corners(text: str) -> dict[str, dict[str, int]]:
    panel = _detail_panel_text(text, markers=("球賽編號:", "詳細賽果"))
    corners: dict[str, dict[str, int]] = {}
    for pattern, key in CORNER_PATTERNS:
        if key in corners:
            continue
        m = re.search(pattern, panel or text)
        if m:
            corners[key] = {
                "total": int(m.group(1)),
                "home": int(m.group(2)),
                "away": int(m.group(3)),
            }
    return corners


def _detail_panel_text(text: str, markers: tuple[str, ...] = ("球賽編號:",)) -> str:
    for marker in markers:
        idx = text.find(marker)
        if idx >= 0:
            return text[idx:]
    return text


def _odds_panel_text(text: str) -> str:
    if "最後賠率" not in text and "更新時間" not in text:
        return ""
    anchor = text.find("最後賠率")
    if anchor < 0:
        anchor = 0
    rest = text[anchor:]
    mid = rest.find("球賽編號:")
    return rest[mid:] if mid >= 0 else rest


def _should_skip_block(header: str) -> bool:
    return any(x in header for x in EXCLUDE_ODDS_HEADERS)


def _is_boundary_line(line: str) -> bool:
    if line in ODDS_SECTIONS or _should_skip_block(line):
        return True
    if line in ("讓球主客和", "同場過關", "同場過關注"):
        return True
    if line.endswith("大細") and line not in ODDS_SECTIONS:
        return True
    if "主客和" in line and line not in ODDS_SECTIONS:
        return True
    return False


def _team_role_from_label(label: str) -> str | None:
    if label in TEAM_LABELS_HOME:
        return "home"
    if label in TEAM_LABELS_AWAY:
        return "away"
    return None


def _parse_team_ou_entries(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current_team: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line in ODDS_SECTIONS:
            break
        role = _team_role_from_label(line)
        if role is not None:
            current_team = role
            i += 1
            continue
        if line in ("球數", "大", "細", "No.", "Over", "Under"):
            i += 1
            continue
        if line.startswith("[") and current_team and i + 2 < len(lines):
            over = lines[i + 1].strip()
            under = lines[i + 2].strip()
            if re.fullmatch(r"\d+\.?\d*", over) and re.fullmatch(r"\d+\.?\d*", under):
                items.append(
                    {
                        "team": current_team,
                        "line": line,
                        "over_odds": float(over),
                        "under_odds": float(under),
                    }
                )
                i += 3
                continue
        i += 1
    return items


def _section_line_indices(lines: list[str]) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped not in ODDS_SECTIONS:
            continue
        if _should_skip_block(stripped):
            continue
        if i + 1 < len(lines) and _should_skip_block(lines[i + 1]):
            continue
        hits.append((stripped, i))
    return hits


def _parse_section_entries(name: str, lines: list[str]) -> list[dict[str, Any]]:
    if name in ("主客和", "半場主客和"):
        selections: list[str] = []
        odds_vals: list[float] = []
        for line in lines:
            if _is_boundary_line(line):
                break
            if re.fullmatch(r"\d+\.?\d*", line):
                odds_vals.append(float(line))
            elif not line.startswith("[") and line not in ("球數", "大", "細"):
                selections.append(line)
        return [
            {"selection": sel, "odds": odd}
            for sel, odd in zip(selections, odds_vals)
        ]

    if name in ("讓球", "半場讓球"):
        items = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if _is_boundary_line(line):
                break
            if line.startswith("[") and i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if re.fullmatch(r"\d+\.?\d*", nxt):
                    items.append({"line": line, "odds": float(nxt)})
                    i += 2
                    continue
            i += 1
        return items

    if name in TEAM_OU_SECTIONS:
        return _parse_team_ou_entries(lines)

    if "大細" in name:
        items = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if _is_boundary_line(line):
                break
            if line in ("球數", "大", "細"):
                i += 1
                continue
            if line.startswith("[") and i + 2 < len(lines):
                over = lines[i + 1].strip()
                under = lines[i + 2].strip()
                if re.fullmatch(r"\d+\.?\d*", over) and re.fullmatch(r"\d+\.?\d*", under):
                    items.append(
                        {
                            "line": line,
                            "over_odds": float(over),
                            "under_odds": float(under),
                        }
                    )
                    i += 3
                    continue
            i += 1
        return items

    return []


def parse_odds_sections(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse closing odds from detail panel body text after % click."""
    panel = _odds_panel_text(text)
    if not panel:
        return {}

    lines = [ln.strip() for ln in panel.split("\n") if ln.strip()]
    sections = _section_line_indices(lines)
    odds: dict[str, list[dict[str, Any]]] = {}

    for idx, (name, start) in enumerate(sections):
        end = sections[idx + 1][1] if idx + 1 < len(sections) else len(lines)
        block = lines[start + 1 : end]
        trimmed: list[str] = []
        for line in block:
            if _is_boundary_line(line):
                break
            trimmed.append(line)
        entries = _parse_section_entries(name, trimmed)
        if entries:
            odds[name] = entries

    for name in ODDS_SECTIONS:
        odds.setdefault(name, [])

    return odds


def format_date_dmy(iso_date: str) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY."""
    y, m, d = iso_date.split("-")
    return f"{d}/{m}/{y}"
