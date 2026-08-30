"""Build an odds snapshot (predict_v32-compatible) from the GraphQL API.

Replaces the Playwright board scrape. Output matches the existing
output/odds_snapshots/hkjc-browser-ch-all-<date>-<ts>.json schema so
predict_v32.py / load_target_matches consume it unchanged.

Usage:
  python3 -m pipeline.snapshot_api --date 2026-08-30
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from pipeline.odds_api import fetch_all

ROOT = Path(__file__).resolve().parent.parent
HKT = timezone(timedelta(hours=8))

# oddsType -> (snapshot market name, kind)
POOL_MAP = {
    "HAD": ("主客和", "had"),
    "FHA": ("半場主客和", "had"),
    "HIL": ("入球大細", "ou"),
    "FHL": ("半場入球大細", "ou"),
    "CHL": ("開出角球大細", "ou"),
    "FCH": ("半場開出角球大細", "ou"),
    "HLH": ("球隊入球大細", "team_ou_home"),
    "HLA": ("球隊入球大細", "team_ou_away"),
    "FLH": ("球隊半場入球大細", "team_ou_home"),
    "FLA": ("球隊半場入球大細", "team_ou_away"),
    "CHH": ("球隊開出角球大細", "team_ou_home"),
    "CHA": ("球隊開出角球大細", "team_ou_away"),
    "CFH": ("球隊半場開出角球大細", "team_ou_home"),
    "CFA": ("球隊半場開出角球大細", "team_ou_away"),
}

FAMILIES = [
    "had", "had_ht", "goal_ou", "goal_ou_ht", "corner_ou", "corner_ou_ht",
    "team_goal_ou", "team_goal_ou_ht", "team_corner_ou", "team_corner_ou_ht",
]

EXTRA_FAMILIES = {
    "had_ht": ["FHA"],
    "team_goal_ou": ["HLH", "HLA", "ELH", "ELA"],
    "team_goal_ou_ht": ["FLH", "FLA"],
    "team_corner_ou": ["CHH", "CHA", "CEH", "CEA"],
    "team_corner_ou_ht": ["CFH", "CFA"],
}


def _fmt_line(cond: Any) -> str:
    s = str(cond or "").strip()
    return f"[{s}]" if s and not s.startswith("[") else s


def build_matches(by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fid, m in by_id.items():
        home = (m.get("homeTeam") or {}).get("name_ch") or ""
        away = (m.get("awayTeam") or {}).get("name_ch") or ""
        ko = m.get("kickOffTime") or ""
        md = (m.get("matchDate") or "")[:10]
        try:
            y, mo, d = md.split("-")
            date_str = f"{d}/{mo}/{y}"
        except ValueError:
            date_str = md
        odds: dict[str, list[dict[str, Any]]] = {}
        for pool in m.get("foPools") or []:
            otype = pool.get("oddsType")
            mapped = POOL_MAP.get(otype)
            if not mapped:
                continue
            market_name, kind = mapped
            for ln in pool.get("lines") or []:
                if not ln.get("main"):
                    continue
                combs = ln.get("combinations") or []
                if kind == "had":
                    sels = []
                    for c in combs:
                        try:
                            o = float(c.get("currentOdds"))
                        except (TypeError, ValueError):
                            continue
                        sel = (c.get("selections") or [{}])[0].get("name_ch") or c.get("str")
                        sels.append({"selection": sel, "odds": o})
                    if sels:
                        odds.setdefault(market_name, []).extend(sels)
                else:
                    over = under = None
                    for c in combs:
                        try:
                            o = float(c.get("currentOdds"))
                        except (TypeError, ValueError):
                            continue
                        if c.get("str") == "H":
                            over = o
                        elif c.get("str") == "L":
                            under = o
                    if over and under:
                        row: dict[str, Any] = {"line": _fmt_line(ln.get("condition")),
                                               "over_odds": over, "under_odds": under}
                        if kind == "team_ou_home":
                            row["team"] = "home"
                        elif kind == "team_ou_away":
                            row["team"] = "away"
                        odds.setdefault(market_name, []).append(row)
        if not odds:
            continue
        out.append({
            "date": date_str,
            "match_id": fid,
            "competition": (m.get("tournament") or {}).get("name_ch") or "",
            "teams": f"{home} 對 {away}",
            "kick_off": ko,
            "odds_closing": odds,
        })
    out.sort(key=lambda r: (r.get("kick_off") or "", r.get("match_id") or ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD label for the snapshot")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "output" / "odds_snapshots")
    args = ap.parse_args()

    from pipeline import odds_api
    for k, v in EXTRA_FAMILIES.items():
        odds_api.FAMILIES[k] = v

    by_id = fetch_all(FAMILIES)
    matches = build_matches(by_id)
    now = datetime.now(HKT)
    snap = {
        "snapshot_at": now.isoformat(timespec="seconds"),
        "source": "hkjc-gql",
        "language": "ch",
        "scope": "all",
        "date_range": args.date,
        "match_count": len(matches),
        "markets": sorted({m for mt in matches for m in mt["odds_closing"]}),
        "matches": matches,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%dT%H%M%S")
    out = args.out_dir / f"hkjc-browser-ch-all-{args.date}-{ts}+0800.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(matches)} matches -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
