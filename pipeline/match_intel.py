"""Match intel: predicted lineups, injuries, and recent news headlines.

Sources (no API key):
  - FotMob public JSON: predicted XI + unavailable (injury / suspension)
  - Google News RSS: last ~24h headlines for the fixture

HKJC English names come from odds-history/meta.json (GraphQL). Matching is
fuzzy token overlap — wrong-team attach is worse than a miss, so the score
floor is conservative.

Usage:
  python3 -m pipeline.match_intel --book output/tomorrow_20260830_v32_live.json
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.team_aliases import norm_tokens

ROOT = Path(__file__).resolve().parent.parent
INTEL_DIR = ROOT / "intel"
META_PATH = ROOT / "odds-history" / "meta.json"
HKT = timezone(timedelta(hours=8))

FOTMOB_MATCHES = "https://www.fotmob.com/api/data/matches?date={ymd}"
FOTMOB_DETAIL = "https://www.fotmob.com/api/data/matchDetails?matchId={mid}"
NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,application/rss+xml,text/xml,*/*",
    "Referer": "https://www.fotmob.com/",
}

MIN_SIDE_SCORE = 0.34
MIN_PAIR_SCORE = 0.80


def _get(url: str, *, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str) -> Any:
    return json.loads(_get(url))


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    # insertion into a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif not skipped:
            skipped = True
            j += 1
        else:
            return False
    return True


def _token_hit(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    if min(len(a), len(b)) >= 5 and _edit1(a, b):
        return True
    return False


def name_score(left: str, right: str) -> float:
    ta, tb = norm_tokens(left or ""), norm_tokens(right or "")
    if not ta or not tb:
        return 0.0
    hits = 0
    used: set[str] = set()
    for a in ta:
        for b in tb:
            if b in used:
                continue
            if _token_hit(a, b):
                hits += 1
                used.add(b)
                break
    return hits / max(len(ta), len(tb))


def _fotmob_list(ymd: str) -> list[dict[str, Any]]:
    payload = _get_json(FOTMOB_MATCHES.format(ymd=ymd))
    rows: list[dict[str, Any]] = []
    for lg in payload.get("leagues") or []:
        league = lg.get("name")
        for m in lg.get("matches") or []:
            home = ((m.get("home") or {}).get("name") or (m.get("home") or {}).get("shortName") or "")
            away = ((m.get("away") or {}).get("name") or (m.get("away") or {}).get("shortName") or "")
            rows.append({
                "id": m.get("id"),
                "home": home,
                "away": away,
                "league": league,
                "utc": (m.get("status") or {}).get("utcTime") or m.get("time"),
            })
    return rows


def _candidate_ymds(kickoff: str | None) -> list[str]:
    """FotMob date is UTC calendar day; HKJC kickoff is +08, so try ±1 day."""
    days: list[str] = []
    now = datetime.now(HKT)
    days.append(now.strftime("%Y%m%d"))
    if kickoff:
        try:
            ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            for delta in (-1, 0, 1):
                days.append((ko.astimezone(timezone.utc) + timedelta(days=delta)).strftime("%Y%m%d"))
        except ValueError:
            pass
    # unique, keep order
    seen: set[str] = set()
    out: list[str] = []
    for d in days:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def match_fotmob(home_en: str, away_en: str, fixtures: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_s = 0.0
    for fx in fixtures:
        hs = name_score(home_en, fx.get("home") or "")
        aws = name_score(away_en, fx.get("away") or "")
        if hs < MIN_SIDE_SCORE or aws < MIN_SIDE_SCORE:
            continue
        s = hs + aws
        if s > best_s:
            best_s = s
            best = {**fx, "home_score": round(hs, 3), "away_score": round(aws, 3), "pair_score": round(s, 3)}
    if best is None or best["pair_score"] < MIN_PAIR_SCORE:
        return None
    return best


def _unavail_row(p: dict[str, Any]) -> dict[str, Any]:
    u = p.get("unavailability") or {}
    return {
        "name": p.get("name"),
        "type": u.get("type") or "unavailable",
        "return": u.get("expectedReturn"),
    }


def _lineup_side(team: dict[str, Any]) -> dict[str, Any]:
    starters = []
    for p in team.get("starters") or []:
        if isinstance(p, dict) and p.get("name"):
            starters.append(p["name"])
    return {
        "name": team.get("name"),
        "formation": team.get("formation"),
        "starters": starters,
        "unavailable": [_unavail_row(p) for p in (team.get("unavailable") or []) if isinstance(p, dict)],
    }


def fetch_fotmob_detail(fotmob_id: int | str) -> dict[str, Any]:
    det = _get_json(FOTMOB_DETAIL.format(mid=fotmob_id))
    lu = ((det.get("content") or {}).get("lineup")) or {}
    return {
        "lineup_type": lu.get("lineupType"),
        "source": lu.get("source"),
        "home": _lineup_side(lu.get("homeTeam") or {}),
        "away": _lineup_side(lu.get("awayTeam") or {}),
    }


def fetch_headlines(home_en: str, away_en: str, *, limit: int = 8) -> list[dict[str, str]]:
    q = urllib.parse.quote(f"{home_en} {away_en} injury OR lineup OR \"team news\"")
    raw = _get(NEWS_RSS.format(q=q), timeout=15)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        # Google titles look like "Foo bar - The Athletic"
        if " - " in title and not source:
            title, source = title.rsplit(" - ", 1)
        items.append({"title": title, "source": source, "link": link, "pub": pub})
        if len(items) >= limit:
            break
    return items


def load_meta() -> dict[str, Any]:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    return {}


def intel_for_match(mid: str, meta: dict[str, Any], fixtures_by_day: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    info = meta.get(mid) or {}
    home_en = info.get("home_en") or ""
    away_en = info.get("away_en") or ""
    out: dict[str, Any] = {
        "match_id": mid,
        "home": info.get("home"),
        "away": info.get("away"),
        "home_en": home_en,
        "away_en": away_en,
        "league": info.get("league"),
        "kickoff": info.get("kickoff"),
        "fotmob": None,
        "lineup_type": None,
        "home_xi": None,
        "away_xi": None,
        "headlines": [],
    }
    if not home_en or not away_en:
        return out

    fixtures: list[dict[str, Any]] = []
    for ymd in _candidate_ymds(info.get("kickoff")):
        if ymd not in fixtures_by_day:
            try:
                fixtures_by_day[ymd] = _fotmob_list(ymd)
            except Exception as e:  # noqa: BLE001
                fixtures_by_day[ymd] = []
                out["fotmob_list_error"] = str(e)[:160]
        fixtures.extend(fixtures_by_day[ymd])

    hit = match_fotmob(home_en, away_en, fixtures)
    if hit:
        out["fotmob"] = hit
        try:
            detail = fetch_fotmob_detail(hit["id"])
            out["lineup_type"] = detail.get("lineup_type")
            out["home_xi"] = detail.get("home")
            out["away_xi"] = detail.get("away")
        except Exception as e:  # noqa: BLE001
            out["fotmob_detail_error"] = str(e)[:160]
        time.sleep(0.15)

    try:
        out["headlines"] = fetch_headlines(home_en, away_en)
    except Exception as e:  # noqa: BLE001
        out["news_error"] = str(e)[:160]
    time.sleep(0.2)
    return out


def collect(match_ids: list[str]) -> dict[str, Any]:
    meta = load_meta()
    fixtures_by_day: dict[str, list[dict[str, Any]]] = {}
    matches: dict[str, Any] = {}
    for mid in match_ids:
        matches[mid] = intel_for_match(mid, meta, fixtures_by_day)
    return {
        "ts": datetime.now(HKT).isoformat(timespec="seconds"),
        "n": len(matches),
        "matches": matches,
    }


def format_brief(intel: dict[str, Any]) -> str:
    """Compact block for the LLM prompt."""
    if not intel:
        return "No intel fetched."
    lines: list[str] = []
    for side, key in (("HOME", "home_xi"), ("AWAY", "away_xi")):
        xi = intel.get(key) or {}
        name = xi.get("name") or intel.get("home_en" if side == "HOME" else "away_en") or side
        un = xi.get("unavailable") or []
        un_s = ", ".join(
            f"{u.get('name')} ({u.get('type')}/{u.get('return') or '?'})"
            for u in un[:8]
        ) or "none listed"
        starters = ", ".join((xi.get("starters") or [])[:11]) or "not published"
        lines.append(f"{side} {name} formation {xi.get('formation') or '?'}: XI {starters}")
        lines.append(f"{side} unavailable: {un_s}")
    lt = intel.get("lineup_type")
    if lt:
        lines.append(f"Lineup type: {lt}")
    heads = intel.get("headlines") or []
    if heads:
        lines.append("Headlines:")
        for h in heads[:6]:
            src = f" ({h.get('source')})" if h.get("source") else ""
            lines.append(f"- {h.get('title')}{src}")
    else:
        lines.append("Headlines: none")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=Path, default=None)
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    ids = list(args.ids)
    if args.book and args.book.exists():
        book = json.loads(args.book.read_text(encoding="utf-8"))
        for r in book:
            mid = r.get("match_id")
            if mid and mid not in ids:
                ids.append(mid)
    if not ids:
        print("no match ids", flush=True)
        return 2

    payload = collect(ids)
    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or INTEL_DIR / f"intel-{datetime.now(HKT).strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"intel {payload['n']} matches -> {out}", flush=True)
    for mid, row in payload["matches"].items():
        fm = row.get("fotmob")
        n_un = 0
        for k in ("home_xi", "away_xi"):
            n_un += len((row.get(k) or {}).get("unavailable") or [])
        hit = f"fotmob={fm.get('home')} vs {fm.get('away')} ({fm.get('pair_score')})" if fm else "NO_FOTMOB"
        print(f"  {mid} {row.get('home_en')} vs {row.get('away_en')} | {hit} | unavail={n_un} news={len(row.get('headlines') or [])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
