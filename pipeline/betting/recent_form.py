"""Recent corner/goal form for home and away teams."""

from __future__ import annotations

from datetime import date
from typing import Any

from pipeline.betting.load import parse_match_date, split_teams
from pipeline.betting.models.poisson_total import _corner_split, _goal_split


def _team_side(match: dict[str, Any], team: str) -> str | None:
    pair = split_teams(match.get("teams") or "")
    if pair is None:
        return None
    home, away = pair
    if team == home:
        return "home"
    if team == away:
        return "away"
    return None


def _match_total_value(match: dict[str, Any], *, stat: str, period: str) -> int | None:
    if stat == "corners":
        split = _corner_split(match, period)
        if split is None:
            return None
        total, _, _ = split
        return total
    split = _goal_split(match, period)
    if split is None:
        return None
    total, _, _ = split
    return total


def _stat_value(match: dict[str, Any], *, stat: str, period: str, side: str) -> int | None:
    if stat == "corners":
        split = _corner_split(match, period)
        if split is None:
            return None
        _, home_c, away_c = split
        return home_c if side == "home" else away_c
    split = _goal_split(match, period)
    if split is None:
        return None
    _, home_g, away_g = split
    return home_g if side == "home" else away_g


def recent_team_values(
    history: list[dict[str, Any]],
    *,
    team: str,
    role: str,
    stat: str,
    period: str,
    before: date,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Last N matches where team played in role (home/away) before cutoff date."""
    rows: list[tuple[date, dict[str, Any]]] = []
    for match in history:
        try:
            match_date = parse_match_date(match["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if match_date >= before:
            continue
        pair = split_teams(match.get("teams") or "")
        if pair is None:
            continue
        home, away = pair
        if role == "home" and home != team:
            continue
        if role == "away" and away != team:
            continue
        side = role
        value = _stat_value(match, stat=stat, period=period, side=side)
        if value is None:
            continue
        rows.append((match_date, match))

    rows.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for match_date, match in rows[:limit]:
        side = role
        value = _stat_value(match, stat=stat, period=period, side=side)
        match_total = _match_total_value(match, stat=stat, period=period)
        pair = split_teams(match.get("teams") or "")
        opp = pair[1] if role == "home" and pair else (pair[0] if pair else "")
        out.append(
            {
                "date": match_date.strftime("%d/%m/%Y"),
                "match_id": match.get("match_id", ""),
                "competition": match.get("competition") or "",
                "teams": match.get("teams") or "",
                "opponent": opp,
                "value": value,
                "match_total": match_total,
                "role": role,
            }
        )
    return out


def format_recent(values: list[dict[str, Any]], *, show_match_total: bool = False) -> str:
    if not values:
        return "n/a"
    if show_match_total:
        parts = []
        for row in values:
            total = row.get("match_total")
            if total is not None:
                parts.append(f"{row['value']}/{total}({row['date']})")
            else:
                parts.append(f"{row['value']}({row['date']})")
        return ", ".join(parts)
    return ", ".join(f"{row['value']}({row['date']})" for row in values)


def match_form_summary(
    history: list[dict[str, Any]],
    match: dict[str, Any],
    *,
    before: date | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    pair = split_teams(match.get("teams") or "")
    if pair is None:
        return {}
    home, away = pair
    cutoff = before or parse_match_date(match["date"])

    blocks: dict[str, Any] = {"home_team": home, "away_team": away}
    for stat, period, label in [
        ("corners", "full_time", "corner_ft"),
        ("corners", "half_time", "corner_ht"),
        ("goals", "full_time", "goal_ft"),
        ("goals", "half_time", "goal_ht"),
    ]:
        home_vals = recent_team_values(
            history, team=home, role="home", stat=stat, period=period, before=cutoff, limit=limit
        )
        away_vals = recent_team_values(
            history, team=away, role="away", stat=stat, period=period, before=cutoff, limit=limit
        )
        blocks[f"{label}_home_last{limit}"] = home_vals
        blocks[f"{label}_away_last{limit}"] = away_vals
        blocks[f"{label}_home_summary"] = format_recent(home_vals, show_match_total=True)
        blocks[f"{label}_away_summary"] = format_recent(away_vals, show_match_total=True)
        if stat == "corners":
            blocks[f"{label}_home_summary_team"] = format_recent(home_vals)
            blocks[f"{label}_away_summary_team"] = format_recent(away_vals)
            blocks[f"{label}_home_summary_total"] = format_recent(
                [{**r, "value": r["match_total"]} for r in home_vals if r.get("match_total") is not None]
            ) if home_vals else "n/a"
            blocks[f"{label}_away_summary_total"] = format_recent(
                [{**r, "value": r["match_total"]} for r in away_vals if r.get("match_total") is not None]
            ) if away_vals else "n/a"
        if home_vals:
            blocks[f"{label}_home_avg"] = round(sum(r["value"] for r in home_vals) / len(home_vals), 2)
            if all(r.get("match_total") is not None for r in home_vals):
                blocks[f"{label}_home_match_total_avg"] = round(
                    sum(r["match_total"] for r in home_vals) / len(home_vals), 2
                )
        if away_vals:
            blocks[f"{label}_away_avg"] = round(sum(r["value"] for r in away_vals) / len(away_vals), 2)
            if all(r.get("match_total") is not None for r in away_vals):
                blocks[f"{label}_away_match_total_avg"] = round(
                    sum(r["match_total"] for r in away_vals) / len(away_vals), 2
                )
    return blocks
