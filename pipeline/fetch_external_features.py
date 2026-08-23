#!/usr/bin/env python3
"""Fetch external football features and report HKJC snapshot coverage.

This script is deliberately coverage-first. A provider that cannot match HKJC
matches is useless for this pipeline, no matter how good its marketing page is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.betting.load import split_teams
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.snapshots import latest_snapshot
from pipeline.team_aliases import DEFAULT_ALIAS_PATH, load_team_aliases, norm_tokens, translate_team_name


DEFAULT_PROVIDERS = ["bsd", "bigballs"]

TEAM_ALIASES = load_team_aliases(str(DEFAULT_ALIAS_PATH))


def _resolve_match_teams(match: dict[str, Any]) -> tuple[str, str] | None:
    """Prefer teams_en / home_en+away_en; fall back to alias-translated Chinese names."""
    if match.get("home_en") and match.get("away_en"):
        return translate_team_name(str(match["home_en"])), translate_team_name(str(match["away_en"]))
    en_pair = split_teams(match.get("teams_en") or "")
    if en_pair:
        return translate_team_name(en_pair[0]), translate_team_name(en_pair[1])
    pair = split_teams(match.get("teams") or "")
    if not pair:
        return None
    return translate_team_name(pair[0]), translate_team_name(pair[1])


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    provider_id: str
    home: str
    away: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class MatchResult:
    match_id: str
    teams: str
    provider: str | None
    provider_id: str | None
    provider_home: str | None
    provider_away: str | None
    score: float
    status: str
    features: dict[str, float]


def _request_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "events", "matches", "fixtures"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _items(value)
            if nested:
                return nested
    return []


def _nested_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "team_name", "display_name", "short_name", "title"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def _field(obj: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in obj:
            name = _nested_name(obj[key])
            if name:
                return name
    return None


def _extract_teams(event: dict[str, Any]) -> tuple[str, str] | None:
    home = _field(event, ("home", "home_team", "homeTeam", "localteam", "local_team", "team_home"))
    away = _field(event, ("away", "away_team", "awayTeam", "visitorteam", "visitor_team", "team_away"))
    if home and away:
        return home, away

    for key in ("name", "event_name", "fixture", "title", "match"):
        value = event.get(key)
        if not isinstance(value, str):
            continue
        for sep in (" vs ", " v ", " - ", " @ "):
            if sep in value:
                left, right = value.split(sep, 1)
                if left.strip() and right.strip():
                    return left.strip(), right.strip()
    return None


def _event_id(event: dict[str, Any]) -> str:
    for key in ("bsd_id", "id", "event_id", "match_id", "fixture_id", "mod_id"):
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _norm_tokens(name: str) -> set[str]:
    return norm_tokens(name, TEAM_ALIASES)


def _similarity(a: str, b: str) -> float:
    ta = _norm_tokens(a)
    tb = _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    score = inter / union if union else 0.0
    if a.casefold() in b.casefold() or b.casefold() in a.casefold():
        score = max(score, 0.85)
    return score


def _merge_dicts(*values: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for value in values:
        merged.update(value)
    return merged


def _bsd_detail(base: str, event_id: str, suffix: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    if not event_id:
        return {}
    url = f"{base}/api/v2/events/{event_id}/{suffix}"
    try:
        payload = _request_json(url, headers=headers, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _enrich_bsd_event(base: str, item: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    event_id = _event_id(item)
    details = _bsd_detail(base, event_id, "", headers, timeout)
    prediction = _bsd_detail(base, event_id, "prediction/", headers, timeout)
    lineups = _bsd_detail(base, event_id, "lineups/", headers, timeout)
    enriched = _merge_dicts(item, {"_bsd_details": details, "_bsd_prediction": prediction, "_bsd_lineups": lineups})
    return enriched


def _match_score(hkjc_home: str, hkjc_away: str, event: ProviderEvent) -> float:
    direct = (_similarity(hkjc_home, event.home) + _similarity(hkjc_away, event.away)) / 2
    swapped = (_similarity(hkjc_home, event.away) + _similarity(hkjc_away, event.home)) / 2
    return max(direct, swapped)


def _match_date_iso(match: dict[str, Any]) -> str | None:
    raw = match.get("date")
    if not isinstance(raw, str):
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _date_values(matches: list[dict[str, Any]]) -> list[str]:
    dates = sorted({d for m in matches if (d := _match_date_iso(m))})
    return dates


def _bsd_fetch_dates(dates: list[str]) -> list[str]:
    """HKJC board dates often lag BSD kickoff dates (e.g. WC QF on US ET)."""
    expanded: set[str] = set()
    for raw in dates:
        expanded.add(raw)
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        expanded.add((dt - timedelta(days=1)).isoformat())
        expanded.add((dt + timedelta(days=1)).isoformat())
    return sorted(expanded)


def _fetch_bsd_events(dates: list[str], *, token: str | None, timeout: float) -> list[ProviderEvent]:
    headers = {"User-Agent": "openclaw-hkjc-football-agent/1.0"}
    if token:
        headers["Authorization"] = f"Token {token}"
    out: list[ProviderEvent] = []
    base = os.environ.get("BSD_BASE_URL", "https://sports.bzzoiro.com")
    for date in dates:
        # BSD's football API is advertised as free under /api/. The /odds/api/
        # endpoints are a separate add-on and return 402 without the paid plan.
        candidate_urls = [
            f"{base}/api/events/?{urllib.parse.urlencode({'date_from': date, 'date_to': date, 'status': 'upcoming', 'tz': 'Asia/Hong_Kong'})}",
            f"{base}/api/events/?{urllib.parse.urlencode({'date': date, 'status': 'upcoming', 'tz': 'Asia/Hong_Kong'})}",
            f"{base}/api/v2/events/?{urllib.parse.urlencode({'date_from': date, 'date_to': date, 'status': 'upcoming', 'tz': 'Asia/Hong_Kong'})}",
        ]
        loaded = False
        for url in candidate_urls:
            try:
                payload = _request_json(url, headers=headers, timeout=timeout)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"BSD fetch failed for {date}: {exc}", file=sys.stderr)
                continue
            items = _items(payload)
            if not items:
                continue
            loaded = True
            for item in items:
                teams = _extract_teams(item)
                if not teams:
                    continue
                enriched = _enrich_bsd_event(base, item, headers, timeout)
                out.append(ProviderEvent("bsd", _event_id(item), teams[0], teams[1], enriched))
            break
        if not loaded:
            time.sleep(0.1)
    return out


def _fetch_bigballs_events(dates: list[str], *, api_key: str | None, timeout: float) -> list[ProviderEvent]:
    headers = {"User-Agent": "openclaw-hkjc-football-agent/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
    out: list[ProviderEvent] = []
    base = os.environ.get("BIGBALLS_BASE_URL", "https://api.bigballsdata.com")
    for date in dates:
        qs = urllib.parse.urlencode({"sport": "football", "date": date})
        url = f"{base}/v1/matches?{qs}"
        try:
            payload = _request_json(url, headers=headers, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"BigBalls fetch failed for {date}: {exc}", file=sys.stderr)
            continue
        for item in _items(payload):
            teams = _extract_teams(item)
            if not teams:
                continue
            out.append(ProviderEvent("bigballs", _event_id(item), teams[0], teams[1], item))
    return out


def _num(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _first_num(obj: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _num(obj.get(key))
        if value is not None:
            return value
    return None


def _features_from_event(event: ProviderEvent) -> dict[str, float]:
    raw = event.raw
    features: dict[str, float] = {}
    prediction = raw.get("_bsd_prediction") if isinstance(raw.get("_bsd_prediction"), dict) else {}
    markets = prediction.get("markets") if isinstance(prediction.get("markets"), dict) else {}
    expected_goals = markets.get("expected_goals") if isinstance(markets.get("expected_goals"), dict) else {}
    lineups = raw.get("_bsd_lineups") if isinstance(raw.get("_bsd_lineups"), dict) else {}
    detail = raw.get("_bsd_details") if isinstance(raw.get("_bsd_details"), dict) else {}

    home_xg = _first_num(raw, ("home_xg", "xg_home", "homeExpectedGoals"))
    away_xg = _first_num(raw, ("away_xg", "xg_away", "awayExpectedGoals"))
    home_xg = home_xg if home_xg is not None else _num(expected_goals.get("home"))
    away_xg = away_xg if away_xg is not None else _num(expected_goals.get("away"))
    home_shots = _first_num(raw, ("home_shots", "shots_home", "homeShots"))
    away_shots = _first_num(raw, ("away_shots", "shots_away", "awayShots"))
    home_injuries = _first_num(raw, ("home_injuries", "homeInjuries", "home_injury_count"))
    away_injuries = _first_num(raw, ("away_injuries", "awayInjuries", "away_injury_count"))
    unavailable = lineups.get("unavailable_players") if isinstance(lineups.get("unavailable_players"), dict) else {}
    if home_injuries is None and isinstance(unavailable.get("home"), list):
        home_injuries = float(len(unavailable["home"]))
    if away_injuries is None and isinstance(unavailable.get("away"), list):
        away_injuries = float(len(unavailable["away"]))

    lineup_root = lineups.get("lineups") if isinstance(lineups.get("lineups"), dict) else {}
    home_lineup = lineup_root.get("home") if isinstance(lineup_root.get("home"), dict) else {}
    away_lineup = lineup_root.get("away") if isinstance(lineup_root.get("away"), dict) else {}
    home_lineup_strength = _lineup_strength(home_lineup)
    away_lineup_strength = _lineup_strength(away_lineup)
    weather_goal_factor = _weather_goal_factor(detail.get("weather") if isinstance(detail.get("weather"), dict) else {})
    for key, value in (
        ("home_xg", home_xg),
        ("away_xg", away_xg),
        ("home_shots", home_shots),
        ("away_shots", away_shots),
        ("home_injuries", home_injuries),
        ("away_injuries", away_injuries),
        ("home_lineup_strength", home_lineup_strength),
        ("away_lineup_strength", away_lineup_strength),
        ("weather_goal_factor", weather_goal_factor),
    ):
        if value is not None:
            features[key] = value
    return features


def _lineup_strength(lineup: dict[str, Any]) -> float | None:
    confidence = _num(lineup.get("confidence"))
    players = lineup.get("players")
    scores: list[float] = []
    if isinstance(players, list):
        for player in players:
            if isinstance(player, dict) and (score := _num(player.get("ai_score"))) is not None:
                scores.append(score)
    if scores:
        return sum(scores) / len(scores)
    return confidence


def _weather_goal_factor(weather: dict[str, Any]) -> float | None:
    if not weather:
        return None
    factor = 1.0
    desc = str(weather.get("description") or "").casefold()
    wind = _num(weather.get("wind_speed"))
    temp = _num(weather.get("temperature_c"))
    if "rain" in desc or "storm" in desc or "snow" in desc:
        factor -= 0.05
    if wind is not None and wind >= 25:
        factor -= 0.04
    if temp is not None and temp >= 32:
        factor -= 0.03
    return max(0.8, min(1.1, factor))


def _best_match(match: dict[str, Any], events: list[ProviderEvent], *, min_score: float) -> MatchResult:
    teams = match.get("teams") or ""
    pair = _resolve_match_teams(match)
    if pair is None:
        return MatchResult(match.get("match_id", ""), teams, None, None, None, None, 0.0, "bad_hkjc_teams", {})
    scored = sorted(((_match_score(pair[0], pair[1], event), event) for event in events), key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < min_score:
        return MatchResult(match.get("match_id", ""), teams, None, None, None, None, scored[0][0] if scored else 0.0, "unmatched", {})
    score, event = scored[0]
    features = _features_from_event(event)
    status = "matched_with_features" if features else "matched_no_features"
    return MatchResult(match.get("match_id", ""), teams, event.provider, event.provider_id, event.home, event.away, score, status, features)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch external football features and report HKJC match coverage")
    parser.add_argument("--snapshot", type=Path, default=None, help="HKJC snapshot JSON")
    parser.add_argument("--snapshot-date", default=None, help="Auto-detect latest snapshot for YYYY-MM-DD")
    parser.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS), help="Comma-separated providers: bsd,bigballs")
    parser.add_argument("--min-score", type=float, default=0.72, help="Minimum fuzzy team match score")
    parser.add_argument("--min-coverage", type=float, default=0.30, help="Fail if matched feature coverage is below this")
    parser.add_argument("--out", type=Path, default=Path("output/external_features.json"), help="Feature JSON output")
    parser.add_argument("--report-out", type=Path, default=Path("output/external_feature_coverage.json"), help="Coverage report output")
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIAS_PATH, help="Team alias JSON for Chinese→English matching")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds")
    args = parser.parse_args()

    global TEAM_ALIASES
    TEAM_ALIASES = load_team_aliases(str(args.aliases))

    snapshot = args.snapshot or latest_snapshot(date=args.snapshot_date)
    if snapshot is None:
        print("No snapshot found.", file=sys.stderr)
        return 1
    matches = load_target_matches(snapshot)
    dates = _date_values(matches)
    bsd_dates = _bsd_fetch_dates(dates)
    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]

    events: list[ProviderEvent] = []
    if "bsd" in providers:
        events.extend(_fetch_bsd_events(bsd_dates, token=os.environ.get("BSD_API_TOKEN"), timeout=args.timeout))
    if "bigballs" in providers:
        events.extend(_fetch_bigballs_events(dates, api_key=os.environ.get("BIGBALLS_API_KEY"), timeout=args.timeout))

    results = [_best_match(match, events, min_score=args.min_score) for match in matches]
    matched = [r for r in results if r.provider is not None]
    with_features = [r for r in matched if r.features]
    coverage = len(with_features) / len(matches) if matches else 0.0
    match_coverage = len(matched) / len(matches) if matches else 0.0

    feature_rows = [
        {"match_id": r.match_id, "teams": r.teams, **r.features}
        for r in with_features
    ]
    report = {
        "snapshot": str(snapshot),
        "providers": providers,
        "dates": dates,
        "bsd_fetch_dates": bsd_dates if "bsd" in providers else dates,
        "hkjc_matches": len(matches),
        "provider_events": len(events),
        "matched_matches": len(matched),
        "matched_coverage": round(match_coverage, 4),
        "feature_matches": len(with_features),
        "feature_coverage": round(coverage, 4),
        "min_coverage": args.min_coverage,
        "decision": "usable" if coverage >= args.min_coverage else "reject_provider_for_now",
        "matches": [
            {
                "match_id": r.match_id,
                "teams": r.teams,
                "provider": r.provider,
                "provider_id": r.provider_id,
                "provider_teams": f"{r.provider_home} vs {r.provider_away}" if r.provider_home and r.provider_away else "",
                "score": round(r.score, 4),
                "status": r.status,
                "feature_keys": sorted(r.features),
            }
            for r in results
        ],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(feature_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"snapshot={snapshot} providers={','.join(providers)} "
        f"hkjc_matches={len(matches)} provider_events={len(events)} "
        f"matched={len(matched)} feature_matches={len(with_features)} "
        f"feature_coverage={coverage:.2%} decision={report['decision']}"
    )
    print(f"features: {args.out}")
    print(f"report: {args.report_out}")
    return 0 if coverage >= args.min_coverage else 2


if __name__ == "__main__":
    raise SystemExit(main())
