#!/usr/bin/env python3
"""Scrape remaining HKJC June matches via Playwright CDP."""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from hkjc_agent import get_existing_ids, upsert_match

ROOT = Path(__file__).parent
MATCHES_FILE = ROOT / "output" / "matches.json"
URL = "https://bet.hkjc.com/ch/football/results#search"
DATE_RANGE = "01/06/2026 - 30/06/2026"
CDP = "http://127.0.0.1:9222"

JS_SEARCH = """
() => {
  const DATE_RANGE = '01/06/2026 - 30/06/2026';
  const input = document.querySelector('input.date-input') ||
    document.querySelector('input.date-input.matchResults_dateSelect_selected_text');
  if (!input) return { ok: false, error: 'no date input' };
  input.value = DATE_RANGE;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const btn = document.querySelector('[data-testid="matchResults_search_button"]') ||
    [...document.querySelectorAll('button')].find(b => b.innerText.trim() === '搜尋');
  if (!btn) return { ok: false, error: 'no search btn' };
  btn.click();
  return { ok: true };
}
"""

JS_PAGE = """
(n) => {
  const box = document.querySelector('.pagination-box');
  if (!box) return { ok: false };
  const t = Array.from(box.children).find(el => el.innerText.trim() === String(n));
  if (t) { t.click(); return { ok: true, page: n }; }
  return { ok: false };
}
"""

JS_ROW = """
(matchId) => {
  for (const row of document.querySelectorAll('.fb-results-match-results .table-row')) {
    if (row.classList.contains('table-header')) continue;
    const cells = row.querySelectorAll('.table-cell');
    if (cells[1]?.innerText?.trim() !== matchId) continue;
    const leagueImg = cells[2]?.querySelector('img');
    return {
      found: true,
      date: cells[0].innerText.trim(),
      match_id: matchId,
      competition: leagueImg?.getAttribute('alt') || leagueImg?.title || '',
      teams: cells[3].innerText.trim(),
      ht_score: cells[4].innerText.trim(),
      ft_score: cells[5].innerText.trim().split('\\n')[0].trim()
    };
  }
  return { found: false };
}
"""

JS_CLICK_BTN = """
(args) => {
  const [matchId, wantPercent] = args;
  for (const row of document.querySelectorAll('.fb-results-match-results .table-row')) {
    const cells = row.querySelectorAll('.table-cell');
    if (cells[1]?.innerText?.trim() !== matchId) continue;
    for (const btn of row.querySelectorAll('.detail-cell .detail-btn')) {
      const t = btn.innerText.trim();
      if (wantPercent ? t === '%' : t !== '%') {
        btn.click();
        return { ok: true };
      }
    }
  }
  return { ok: false };
}
"""

JS_CORNERS = """
() => {
  const text = document.body.innerText;
  const parseCorner = (label) => {
    const re = new RegExp(label + '[\\\\s\\\\S]{0,80}?(\\\\d+)\\\\s*\\\\(\\\\s*(\\\\d+)\\\\s*:\\\\s*(\\\\d+)\\\\s*\\\\)');
    const m = text.match(re);
    if (!m) return null;
    return { total: +m[1], home: +m[2], away: +m[3] };
  };
  return {
    half_time_corners: parseCorner('半場角球') || parseCorner('半场角球'),
    full_time_corners: parseCorner('全場角球') || parseCorner('全场角球')
  };
}
"""

JS_ODDS = """
() => {
  const WANT = ['主客和','半場主客和','讓球','半場讓球','入球大細','半場入球大細','球隊入球大細','球隊半場入球大細','開出角球大細','半場開出角球大細','球隊開出角球大細','球隊半場開出角球大細'];
  const allText = document.body.innerText;
  const out = {};
  for (let i = 0; i < WANT.length; i++) {
    const name = WANT[i];
    let idx = allText.indexOf(name);
    if (idx < 0) { out[name] = []; continue; }
    let end = allText.length;
    for (let j = i + 1; j < WANT.length; j++) {
      const nxt = allText.indexOf(WANT[j], idx + name.length);
      if (nxt > idx) { end = Math.min(end, nxt); }
    }
    const slice = allText.slice(idx, Math.min(end, idx + 4000));
    if (/即場|同場過關/.test(slice.slice(0, 40))) { out[name] = []; continue; }
    out[name] = slice;
  }
  return out;
}
"""

JS_CLOSE = """
() => {
  const close = document.querySelector('.close-btn, .modal-close, [class*="close"], button[aria-label="Close"]');
  if (close) { close.click(); return { ok: true, via: 'button' }; }
  const overlay = document.querySelector('.modal-overlay, .overlay, [class*="overlay"]');
  if (overlay) { overlay.click(); return { ok: true, via: 'overlay' }; }
  return { ok: false };
}
"""


def parse_odds_sections(raw: dict) -> dict:
    out = {}
    for name, slice in raw.items():
        if not slice:
            out[name] = []
            continue
        if "主客和" in name:
            items = []
            for m in re.finditer(r"([^\n(]+?\(主隊勝\)|和|[^\n(]+?\(客隊勝\))\s*(\d+\.\d+)", slice):
                items.append({"selection": m.group(1).strip(), "odds": float(m.group(2))})
            if not items:
                for m in re.finditer(r"(主隊勝|和|客隊勝)[^\d]*(\d+\.\d+)", slice):
                    items.append({"selection": m.group(1), "odds": float(m.group(2))})
            out[name] = items
        elif "大細" in name:
            items = []
            for m in re.finditer(r"(\[[^\]]+\])\s*(?:大|Over)?\s*(\d+\.\d+)\s*(?:細|Under)?\s*(\d+\.\d+)", slice, re.I):
                items.append({"line": m.group(1), "over_odds": float(m.group(2)), "under_odds": float(m.group(3))})
            if not items:
                lines = re.findall(r"(\[[^\]]+\])", slice)
                odds = [float(x) for x in re.findall(r"\b(\d+\.\d+)\b", slice)]
                oi = 0
                for line in lines:
                    if oi + 1 < len(odds):
                        items.append({"line": line, "over_odds": odds[oi], "under_odds": odds[oi + 1]})
                        oi += 2
            out[name] = items
        else:
            items = []
            for m in re.finditer(r"(\[[^\]]+\]|[-+]?\d+(?:\.\d+)?(?:\/[-+]?\d+(?:\.\d+)?)?)\s+(\d+\.\d+)", slice):
                items.append({"line": m.group(1), "odds": float(m.group(2))})
            out[name] = items
    return out


def ensure_row(page, match_id: str) -> dict | None:
    for pg in (1, 2, 3):
        if pg > 1:
            page.evaluate(JS_PAGE, pg)
            time.sleep(1.5)
        row = page.evaluate(JS_ROW, match_id)
        if row.get("found"):
            return row
    return None


def process_match(page, match_id: str, meta: dict) -> None:
    print(f"Processing {match_id}...")
    page.goto(URL, wait_until="domcontentloaded")
    time.sleep(2)
    page.evaluate(JS_SEARCH)
    time.sleep(3)

    row = ensure_row(page, match_id)
    if not row:
        print(f"  SKIP: row not found for {match_id}")
        return

    page.evaluate(JS_CLICK_BTN, [match_id, False])
    time.sleep(2)
    corners_raw = page.evaluate(JS_CORNERS)
    page.keyboard.press("Escape")
    time.sleep(1)

    page.evaluate(JS_CLICK_BTN, [match_id, True])
    time.sleep(2)
    odds_raw = page.evaluate(JS_ODDS)
    odds = parse_odds_sections(odds_raw)
    page.keyboard.press("Escape")
    time.sleep(0.5)

    corners = {}
    if corners_raw.get("half_time_corners"):
        corners["half_time"] = corners_raw["half_time_corners"]
    if corners_raw.get("full_time_corners"):
        corners["full_time"] = corners_raw["full_time_corners"]

    record = {
        "date": row.get("date") or meta.get("date", ""),
        "match_id": match_id,
        "competition": row.get("competition") or "",
        "teams": row.get("teams") or meta.get("teams", ""),
        "scores": {
            "half_time": row.get("ht_score") or meta.get("ht_score", ""),
            "full_time": row.get("ft_score") or meta.get("ft_score", ""),
        },
        "corners": corners,
        "odds_closing": odds,
    }
    n = upsert_match(record)
    print(f"  OK saved ({n} total) corners={bool(corners)} odds={sum(1 for v in odds.values() if v)}/8")


def main():
    all_matches = {m["match_id"]: m for m in json.loads(MATCHES_FILE.read_text())["matches"]}
    done = get_existing_ids()
    remaining = [mid for mid in all_matches if mid not in done]
    if not remaining:
        print("All matches already recorded.")
        return

    print(f"Remaining: {len(remaining)} -> {', '.join(remaining)}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        for mid in remaining:
            page = context.new_page()
            try:
                process_match(page, mid, all_matches[mid])
            except Exception as e:
                print(f"  ERROR {mid}: {e}")
            finally:
                try:
                    page.close()
                except Exception:
                    pass

    print("Done.")


if __name__ == "__main__":
    main()
