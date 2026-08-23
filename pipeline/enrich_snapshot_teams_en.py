#!/usr/bin/env python3
"""Add English team names (teams_en) to an HKJC snapshot for provider matching."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from pipeline.betting.load import split_teams
from pipeline.betting.recommend import load_target_matches
from pipeline.build_team_aliases import build_alias_map
from pipeline.team_aliases import DEFAULT_ALIAS_PATH, load_team_aliases, translate_team_name


def _resolve_aliases(
    alias_path: Path | None,
    ch_snapshot: Path,
    en_snapshot: Path | None,
) -> dict[str, str]:
    if alias_path and alias_path.is_file():
        return load_team_aliases(alias_path)
    return build_alias_map(ch_snapshot, en_snapshot)


def enrich_snapshot(
    snapshot: Path,
    *,
    en_snapshot: Path | None = None,
    alias_path: Path | None = DEFAULT_ALIAS_PATH,
    aliases: dict[str, str] | None = None,
) -> dict:
    alias_map = aliases if aliases is not None else _resolve_aliases(alias_path, snapshot, en_snapshot)
    en_by_id: dict[str, dict] = {}
    if en_snapshot and en_snapshot.is_file():
        en_by_id = {m["match_id"]: m for m in load_target_matches(en_snapshot)}

    raw = json.loads(snapshot.read_text(encoding="utf-8"))
    payload = deepcopy(raw)
    matches = payload.get("matches")
    if not isinstance(matches, list):
        matches = load_target_matches(snapshot)
        payload = {"matches": matches, "meta": payload.get("meta", {})}

    enriched = 0
    for match in matches:
        if not isinstance(match, dict):
            continue
        match_id = match.get("match_id", "")
        en_match = en_by_id.get(match_id)
        if en_match and (en_teams := en_match.get("teams")):
            match["teams_en"] = en_teams
            en_pair = split_teams(en_teams)
            if en_pair:
                match["home_en"] = translate_team_name(en_pair[0], alias_map)
                match["away_en"] = translate_team_name(en_pair[1], alias_map)
            enriched += 1
            continue

        pair = split_teams(match.get("teams") or "")
        if not pair:
            continue
        home_en = translate_team_name(pair[0], alias_map)
        away_en = translate_team_name(pair[1], alias_map)
        match["home_en"] = home_en
        match["away_en"] = away_en
        match["teams_en"] = f"{home_en} 對 {away_en}"
        enriched += 1

    meta = payload.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = {}
        payload["meta"] = meta
    meta["teams_en_enriched"] = True
    meta["teams_en_source"] = str(en_snapshot) if en_snapshot else str(alias_path or DEFAULT_ALIAS_PATH)
    meta["teams_en_count"] = enriched
    meta["teams_en_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich HKJC snapshot with English team names")
    parser.add_argument("--snapshot", type=Path, required=True, help="Input HKJC snapshot JSON")
    parser.add_argument("--en-snapshot", type=Path, default=None, help="English HKJC snapshot for match_id pairing")
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIAS_PATH, help="Team alias JSON")
    parser.add_argument("--out", type=Path, default=None, help="Output path (default: sibling *-teams-en-*.json)")
    args = parser.parse_args()

    payload = enrich_snapshot(args.snapshot, en_snapshot=args.en_snapshot, alias_path=args.aliases)
    if args.out:
        out = args.out
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = args.snapshot.stem.replace("-all-", "-teams-en-all-")
        out = args.snapshot.with_name(f"{stem}-{stamp}.json")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    count = payload.get("meta", {}).get("teams_en_count", 0)
    print(f"wrote {out} (teams_en on {count} matches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
