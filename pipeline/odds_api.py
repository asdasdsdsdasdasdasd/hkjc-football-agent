"""Direct HKJC football odds client over the GraphQL endpoint the SPA uses.

Pure HTTP (no Playwright). The endpoint hash-whitelists the SPA query string,
so SPA_QUERY below is the verbatim captured query; only variables may change.

Odds type families (must be polled separately; cross-family combos rejected):
  had          HAD/EHA  主客和
  goal_ou      HIL/EHL  入球大細 (FT)
  goal_ou_ht   FHL      半場入球大細
  corner_ou    CHL/ECH  開出角球大細 (FT)
  corner_ou_ht FCH      半場開出角球大細
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.request
from typing import Any

URL = "https://info.cld.hkjc.com/graphql/base/"

HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://bet.hkjc.com",
    "Referer": "https://bet.hkjc.com/ch/football/hil",
}

SPA_QUERY = '\n      query matchList($startIndex: Int, $endIndex: Int,$startDate: String, $endDate: String, $matchIds: [String], $tournIds: [String], $fbOddsTypes: [FBOddsType]!, $fbOddsTypesM: [FBOddsType]!, $inplayOnly: Boolean, $featuredMatchesOnly: Boolean, $frontEndIds: [String], $earlySettlementOnly: Boolean, $showAllMatch: Boolean) {\n        matches(startIndex: $startIndex,endIndex: $endIndex, startDate: $startDate, endDate: $endDate, matchIds: $matchIds, tournIds: $tournIds, fbOddsTypes: $fbOddsTypesM, inplayOnly: $inplayOnly, featuredMatchesOnly: $featuredMatchesOnly, frontEndIds: $frontEndIds, earlySettlementOnly: $earlySettlementOnly, showAllMatch: $showAllMatch) {\n          id\n          frontEndId\n          matchDate\n          kickOffTime\n          status\n          updateAt\n          sequence\n          esIndicatorEnabled\n          homeTeam {\n            id\n            name_en\n            name_ch\n          }\n          awayTeam {\n            id\n            name_en\n            name_ch\n          }\n          tournament {\n            id\n            frontEndId\n            nameProfileId\n            isInteractiveServiceAvailable\n            code\n            name_en\n            name_ch\n          }\n          isInteractiveServiceAvailable\n          inplayDelay\n          venue {\n            code\n            name_en\n            name_ch\n          }\n          tvChannels {\n            code\n            name_en\n            name_ch\n          }\n          liveEvents {\n            id\n            code\n          }\n          featureStartTime\n          featureMatchSequence\n          poolInfo {\n            normalPools\n            inplayPools\n            sellingPools\n            ntsInfo\n            entInfo\n            definedPools\n            ngsInfo {\n              str\n              name_en\n              name_ch\n              instNo\n            }\n            agsInfo {\n              str\n              name_en\n              name_ch\n            }\n          }\n          runningResult {\n            homeScore\n            awayScore\n            corner\n            homeCorner\n            awayCorner\n          }\n          runningResultExtra {\n            homeScore\n            awayScore\n            corner\n            homeCorner\n            awayCorner\n          }\n          adminOperation {\n            remark {\n              typ\n            }\n          }\n          foPools(fbOddsTypes: $fbOddsTypes) {\n            id\n            status\n            oddsType\n            instNo\n            inplay\n            name_ch\n            name_en\n            updateAt\n            expectedSuspendDateTime\n            lines {\n              lineId\n              status\n              condition\n              main\n              combinations {\n                combId\n                str\n                status\n                offerEarlySettlement\n                currentOdds\n                selections {\n                  selId\n                  str\n                  name_ch\n                  name_en\n                }\n              }\n            }\n          }\n        }\n      }\n      '

BASE_VARIABLES: dict[str, Any] = {
    "featuredMatchesOnly": False,
    "startDate": None,
    "endDate": None,
    "tournIds": None,
    "matchIds": None,
    "tournId": None,
    "tournProfileId": None,
    "subType": None,
    "frontEndIds": None,
    "earlySettlementOnly": False,
    "showAllMatch": False,
    "tday": None,
    "tIdList": None,
}

FAMILIES: dict[str, list[str]] = {
    "had": ["HAD", "EHA"],
    "goal_ou": ["HIL", "EHL"],
    "goal_ou_ht": ["FHL"],
    "corner_ou": ["CHL", "ECH"],
    "corner_ou_ht": ["FCH"],
}


def fetch_family(family: str, *, start: int = 1, end: int = 200, retries: int = 3) -> list[dict[str, Any]]:
    types = FAMILIES[family]
    variables = dict(BASE_VARIABLES)
    variables.update({"startIndex": start, "endIndex": end, "fbOddsTypes": types, "fbOddsTypesM": types})
    payload = json.dumps({"query": SPA_QUERY, "variables": variables}).encode()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(URL, data=payload, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            data = json.loads(raw)
            if data.get("errors"):
                raise RuntimeError(f"graphql errors: {data['errors']}")
            return (data.get("data") or {}).get("matches") or []
        except Exception as e:  # noqa: BLE001 - retry transient failures
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch_family({family}) failed after {retries} tries: {last_err}")


def fetch_all(families: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Fetch families, merge pools by match frontEndId."""
    families = families or list(FAMILIES)
    by_id: dict[str, dict[str, Any]] = {}
    for fam in families:
        for m in fetch_family(fam):
            fid = m.get("frontEndId") or m.get("id")
            if not fid:
                continue
            slot = by_id.setdefault(fid, {**m, "foPools": []})
            slot["foPools"].extend(m.get("foPools") or [])
    return by_id


def iter_odds_rows(match: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a match's pools into odds rows.

    side: H/L for O/U pools (high=over / low=under), H/D/A for HAD.
    """
    rows: list[dict[str, Any]] = []
    mid = match.get("frontEndId")
    for pool in match.get("foPools") or []:
        otype = pool.get("oddsType")
        for ln in pool.get("lines") or []:
            cond = ln.get("condition")
            for c in ln.get("combinations") or []:
                odds = c.get("currentOdds")
                try:
                    odds_f = float(odds)
                except (TypeError, ValueError):
                    continue
                rows.append({
                    "match_id": mid,
                    "market": otype,
                    "line": cond,
                    "side": c.get("str"),
                    "odds": odds_f,
                    "main": bool(ln.get("main")),
                    "pool_status": pool.get("status"),
                    "line_status": ln.get("status") or c.get("status"),
                })
    return rows
