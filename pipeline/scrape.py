#!/usr/bin/env python3
"""Playwright scraper: corners (resultDetails) + closing odds (lastOdds) per match."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_RECORDS_JSON,
    DEFAULT_RECORDS_JSONL,
    DETAIL_WAIT_MS,
    MATCH_DELAY_SEC,
    PAGE_LOAD_TIMEOUT_MS,
    RESULTS_URL,
    SEARCH_DATE_INPUT_TIMEOUT_MS,
    SEARCH_DAY_ATTEMPTS,
    SEARCH_DAY_BACKOFF_SEC,
    SEARCH_POST_LOAD_MS,
    SEARCH_REACT_READY_MS,
    SEARCH_RESULTS_TIMEOUT_MS,
    SEARCH_TAB_SETTLE_MS,
)
from pipeline.db import connect, fetch_pending, mark_done, mark_error, status_summary  # noqa: E402
from pipeline.parsers import format_date_dmy, parse_corners, parse_odds_sections  # noqa: E402
from pipeline.storage import upsert_record  # noqa: E402

LOG_DIR = ROOT / "logs"
VIEWPORT = {"width": 1400, "height": 1200}


class DaySearchError(Exception):
    """HKJC date search failed after retries; matches stay pending."""

    def __init__(self, iso_date: str):
        self.iso_date = iso_date
        super().__init__(f"Date search failed for {iso_date}")


def setup_logging(worker_id: int | None = None) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if worker_id is not None:
        log_path = LOG_DIR / f"pool-worker-{worker_id:02d}.log"
    else:
        log_path = LOG_DIR / f"pipeline-{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    return logging.getLogger("scrape")


JS_REACT_SEARCH = """
// HKJC datepicker input is React-controlled; UI clicks fail headless. Drive the
// search form's internal React state via onChangeSearchParams.
// Component name is minified and changes (was "Jf", later "Kf") — match by props.
([startIso, endIso]) => {
  const input = document.querySelector('input.date-input');
  if (!input) return { ok: false, error: 'no date input' };
  const fiberKey = Object.keys(input).find(k => k.startsWith('__reactFiber'));
  let fiber = input[fiberKey];
  for (let i = 0; i < 60 && fiber; i++) {
    const props = fiber.memoizedProps || fiber.pendingProps || {};
    if (typeof props.onChangeSearchParams === 'function') {
      const sp = props.searchParams || {};
      props.onChangeSearchParams({
        ...sp,
        isSearch: true,
        startDate: startIso,
        endDate: endIso,
        pageNum: 1,
      });
      if (typeof props.handleClickSearchBtn === 'function') {
        props.handleClickSearchBtn();
      } else {
        document.querySelector('[data-testid="matchResults_search_button"]')?.click();
      }
      return { ok: true, via: String(fiber.elementType?.name || fiber.elementType?.displayName || i) };
    }
    fiber = fiber.return;
  }
  return { ok: false, error: 'search component not found' };
}
"""

JS_CLICK_PAGE = """
(n) => {
  const box = document.querySelector('.pagination-box');
  if (!box) return false;
  const target = Array.from(box.children).find(el => el.innerText.trim() === String(n));
  if (target) { target.click(); return true; }
  const arrow = box.querySelector('.arrow-icon-default-right');
  if (arrow && !arrow.closest('.disable')) { arrow.click(); return true; }
  return false;
}
"""

JS_LIST_ROWS = """
() => {
  const out = [];
  for (const row of document.querySelectorAll('.fb-results-match-results .table-row')) {
    if (row.classList.contains('table-header')) continue;
    const cells = row.querySelectorAll('.table-cell');
    const matchId = cells[1]?.innerText?.trim();
    if (!matchId) continue;
    const leagueImg = cells[2]?.querySelector('img');
    out.push({
      match_id: matchId,
      date: cells[0]?.innerText?.trim() || '',
      competition: leagueImg?.getAttribute('alt') || leagueImg?.title || '',
      teams: cells[3]?.innerText?.trim() || '',
      ht_score: cells[4]?.innerText?.trim() || '',
      ft_score: cells[5]?.innerText?.trim().split('\\n')[0].trim() || ''
    });
  }
  return out;
}
"""

JS_ROW_DATA = """
(matchId) => {
  for (const row of document.querySelectorAll('.fb-results-match-results .table-row')) {
    if (row.classList.contains('table-header')) continue;
    const cells = row.querySelectorAll('.table-cell');
    if (cells[1]?.innerText?.trim() !== matchId) continue;
    const leagueImg = cells[2]?.querySelector('img');
    return {
      found: true,
      date: cells[0]?.innerText?.trim() || '',
      competition: leagueImg?.getAttribute('alt') || leagueImg?.title || '',
      teams: cells[3]?.innerText?.trim() || '',
      ht_score: cells[4]?.innerText?.trim() || '',
      ft_score: cells[5]?.innerText?.trim().split('\\n')[0].trim() || ''
    };
  }
  return { found: false };
}
"""

JS_CLICK_DETAIL_BTN = """
(args) => {
  const [matchId, wantPercent] = args;
  const testid = wantPercent ? 'lastOdds' : 'resultDetails';
  const cell = document.querySelector(`[data-testid="matchResults_${matchId}_${testid}"]`);
  if (cell) {
    const btn = cell.querySelector('.detail-btn') || cell;
    btn.click();
    return { ok: true, via: 'testid' };
  }
  for (const row of document.querySelectorAll('.fb-results-match-results .table-row')) {
    const cells = row.querySelectorAll('.table-cell');
    if (cells[1]?.innerText?.trim() !== matchId) continue;
    for (const btn of row.querySelectorAll('.detail-cell .detail-btn')) {
      const t = btn.innerText.trim();
      if (wantPercent ? t === '%' : t !== '%') {
        btn.click();
        return { ok: true, via: 'row' };
      }
    }
  }
  return { ok: false };
}
"""


class Scraper:
    def __init__(self, page, log: logging.Logger):
        self.page = page
        self.log = log
        self._active_date: str | None = None  # YYYY-MM-DD single-day filter

    def goto_results(self) -> None:
        self.page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        try:
            self.page.wait_for_selector(
                ".fb-results-match-results .table-row, [data-testid*='_resultDetails'], input.date-input",
                timeout=30000,
            )
        except Exception:
            self.page.wait_for_timeout(3000)

    def _ensure_search_tab(self) -> None:
        self.page.evaluate(
            """() => {
              const tab = [...document.querySelectorAll('*')].find(
                el => el.innerText?.trim() === '搜尋' && el.classList?.contains('cursor-pointer')
              );
              tab?.click();
            }"""
        )
        self.page.wait_for_timeout(SEARCH_TAB_SETTLE_MS)

    def search_date_range(self, start_iso: str, end_iso: str) -> bool:
        """Set HKJC search params via React state (works headless; bypasses datepicker UI)."""
        self.page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        self._ensure_search_tab()
        try:
            self.page.wait_for_selector(
                "input.date-input", state="attached", timeout=SEARCH_DATE_INPUT_TIMEOUT_MS
            )
        except Exception:
            self.log.warning("Date input not found on results page")
            return False
        # Step 3: let React hydrate before walking the fiber tree
        self.page.wait_for_timeout(SEARCH_REACT_READY_MS)
        result = self.page.evaluate(JS_REACT_SEARCH, [start_iso, end_iso])
        if not result.get("ok"):
            self.log.warning("React search failed: %s", result.get("error"))
            return False
        try:
            self.page.wait_for_function(
                """
                () => {
                  const rows = document.querySelectorAll(
                    '.fb-results-match-results .table-row:not(.table-header)'
                  ).length;
                  const summary = document.body.innerText || '';
                  return rows > 0 || summary.includes('共找到');
                }
                """,
                timeout=SEARCH_RESULTS_TIMEOUT_MS,
            )
        except Exception:
            self.log.warning("No results loaded for %s - %s", start_iso, end_iso)
            return False
        self.page.wait_for_timeout(SEARCH_POST_LOAD_MS)
        return True

    def search_day(self, iso_date: str) -> None:
        if self._active_date == iso_date:
            return
        for attempt in range(SEARCH_DAY_ATTEMPTS):
            if self.search_date_range(iso_date, iso_date):
                self._active_date = iso_date
                self.log.info("Search active for %s", iso_date)
                return
            wait = SEARCH_DAY_BACKOFF_SEC * (2**attempt)
            self.log.warning(
                "Date search attempt %d/%d failed for %s, retry in %.0fs",
                attempt + 1,
                SEARCH_DAY_ATTEMPTS,
                iso_date,
                wait,
            )
            self._active_date = None
            time.sleep(wait)
        raise DaySearchError(iso_date)

    def testid_exists(self, match_id: str, kind: str = "resultDetails") -> bool:
        sel = f'[data-testid="matchResults_{match_id}_{kind}"]'
        return self.page.locator(sel).count() > 0

    def row_exists(self, match_id: str) -> bool:
        return bool(self.page.evaluate(JS_ROW_DATA, match_id).get("found"))

    def go_to_results_page(self, page_num: int) -> bool:
        """Navigate the results table to a 1-based page index."""
        if page_num < 1:
            page_num = 1
        box_exists = self.page.locator(".pagination-box").count() > 0
        if not box_exists:
            return page_num == 1
        if page_num == 1:
            clicked = self.page.evaluate(JS_CLICK_PAGE, 1)
            self.page.wait_for_timeout(800 if clicked else 200)
            return True
        if not self.page.evaluate(JS_CLICK_PAGE, 1):
            return False
        self.page.wait_for_timeout(800)
        for p in range(2, page_num + 1):
            if not self.page.evaluate(JS_CLICK_PAGE, p):
                return False
            self.page.wait_for_timeout(1200)
        return True

    def find_on_pages(self, match_id: str, max_pages: int = 50) -> bool:
        self.go_to_results_page(1)
        for page_num in range(1, max_pages + 1):
            if self.testid_exists(match_id) or self.row_exists(match_id):
                return True
            if page_num >= max_pages:
                break
            clicked = self.page.evaluate(JS_CLICK_PAGE, page_num + 1)
            if not clicked:
                break
            self.page.wait_for_timeout(1200)
        return False

    def locate_match_row(
        self,
        match_id: str,
        *,
        results_page: int | None = None,
    ) -> bool:
        if results_page is not None:
            if not self.go_to_results_page(results_page):
                return False
            return self.testid_exists(match_id) or self.row_exists(match_id)
        return self.find_on_pages(match_id)

    def click_detail(self, match_id: str, kind: str) -> None:
        want_percent = kind == "lastOdds"
        ok = self.page.evaluate(JS_CLICK_DETAIL_BTN, [match_id, want_percent])
        if not ok.get("ok"):
            raise RuntimeError(f"Could not click {kind} for {match_id}")
        self.page.wait_for_function(
            "() => location.hash.includes('detail')",
            timeout=DETAIL_WAIT_MS,
        )
        marker = "最後賠率" if want_percent else "半場比數"
        self.page.wait_for_function(
            f"""() => {{
                const t = document.body.innerText || '';
                return t.includes('球賽編號:') && t.includes('{match_id}') && t.includes('{marker}');
            }}""",
            timeout=DETAIL_WAIT_MS,
        )
        self.page.wait_for_timeout(500)

    def detail_text(self) -> str:
        return self.page.inner_text("body")

    def click_football_database(self) -> bool:
        """Open 足智資料庫 tab on match detail — corner counts live here, not on 賽果."""
        clicked = self.page.evaluate(
            """() => {
              const el = [...document.querySelectorAll('*')].find(
                e => e.innerText?.trim() === '足智資料庫' && e.children.length === 0
              );
              if (!el) return false;
              el.click();
              return true;
            }"""
        )
        if not clicked:
            return False
        try:
            self.page.wait_for_function(
                """() => (document.body.innerText || '').includes('全場開出角球')""",
                timeout=min(DETAIL_WAIT_MS, 5000),
            )
            self.page.wait_for_timeout(300)
            return True
        except Exception:
            # Some matches never publish corner counts; keep score + odds scrape.
            self.log.warning("足智資料庫 opened but corner lines not found")
            return False

    def back_to_list(self, iso_date: str) -> None:
        """Leave detail view and restore the single-day search results table."""
        self._active_date = None
        self.search_day(iso_date)

    def row_data(self, match_id: str) -> dict[str, Any] | None:
        data = self.page.evaluate(JS_ROW_DATA, match_id)
        return data if data.get("found") else None

    def list_matches_on_results(self, max_pages: int = 50) -> list[dict[str, Any]]:
        """Collect all match rows currently visible across paginated results."""
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page_num in range(1, max_pages + 1):
            for row in self.page.evaluate(JS_LIST_ROWS):
                mid = row.get("match_id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                found.append({**row, "results_page": page_num})
            if page_num >= max_pages:
                break
            clicked = self.page.evaluate(JS_CLICK_PAGE, page_num + 1)
            if not clicked:
                break
            self.page.wait_for_timeout(1200)
        self.go_to_results_page(1)
        return found


def build_record(
    row: Any,
    corners: dict,
    odds: dict,
    row_data: dict[str, Any] | None,
) -> dict[str, Any]:
    iso_date = row["match_date"]
    dmy = format_date_dmy(iso_date)
    rd = row_data or {}
    ht = rd.get("ht_score") or row["ht_score"] or ""
    ft = rd.get("ft_score") or row["ft_score"] or ""
    return {
        "date": rd.get("date") or dmy,
        "match_id": row["match_id"],
        "competition": rd.get("competition") or row["competition"] or "",
        "teams": rd.get("teams") or row["teams"] or "",
        "scores": {"half_time": ht, "full_time": ft},
        "corners": corners,
        "odds_closing": odds,
    }


def scrape_match(
    scraper: Scraper,
    row: Any,
    log: logging.Logger,
    *,
    skip_search: bool = False,
    results_page: int | None = None,
    skip_corners: bool = False,
) -> dict[str, Any]:
    match_id = row["match_id"]
    iso_date = row["match_date"]
    log.info("%s locate %s page=%s", match_id, iso_date, results_page or "?")

    if not skip_search:
        scraper.search_day(iso_date)
    if not scraper.locate_match_row(match_id, results_page=results_page):
        raise RuntimeError(f"Match {match_id} not found on results for {iso_date}")

    row_data = scraper.row_data(match_id)

    corners: dict = {}
    if skip_corners:
        log.info("%s corners skipped by flag", match_id)
    else:
        log.info("%s corners", match_id)
        try:
            scraper.click_detail(match_id, "resultDetails")
            scraper.click_football_database()
            corners = parse_corners(scraper.detail_text())
        except Exception as exc:
            log.warning("%s corners skipped: %s", match_id, exc)
        finally:
            try:
                scraper.back_to_list(iso_date)
            except Exception:
                scraper.search_day(iso_date)
        if not scraper.locate_match_row(match_id, results_page=results_page):
            raise RuntimeError(f"Match {match_id} lost after corners back-nav")

    log.info("%s odds", match_id)
    scraper.click_detail(match_id, "lastOdds")
    odds = parse_odds_sections(scraper.detail_text())
    scraper.back_to_list(iso_date)

    return build_record(row, corners, odds, row_data)


def scrape_calendar_day(
    scraper: Scraper,
    iso_date: str,
    log: logging.Logger,
    *,
    output_jsonl: Path,
    date_range: str | None = None,
    skip_corners: bool = False,
) -> tuple[int, int]:
    """Scrape every match listed on HKJC results for one calendar day (web-driven)."""
    dmy = format_date_dmy(iso_date)
    log.info("Calendar day %s: loading results table", iso_date)
    scraper.search_day(iso_date)
    web_rows = scraper.list_matches_on_results()
    if not web_rows:
        log.warning("Calendar day %s: no matches on results page", iso_date)
        return 0, 0

    log.info("Calendar day %s: scraping %d matches from web", iso_date, len(web_rows))
    done = 0
    errors = 0
    for wr in web_rows:
        match_id = wr["match_id"]
        results_page = int(wr.get("results_page") or 1)
        row = {
            "match_id": match_id,
            "match_date": iso_date,
            "competition": wr.get("competition") or "",
            "teams": wr.get("teams") or "",
            "ht_score": wr.get("ht_score") or "",
            "ft_score": wr.get("ft_score") or "",
        }
        try:
            scraper.search_day(iso_date)
            record = scrape_match(
                scraper,
                row,
                log,
                skip_search=True,
                results_page=results_page,
                skip_corners=skip_corners,
            )
            record["date"] = wr.get("date") or dmy
            upsert_record(
                record,
                jsonl_path=output_jsonl,
                date_range=date_range,
                json_only=True,
            )
            done += 1
            log.info(
                "%s done corners=%s odds_sections=%d",
                match_id,
                bool(record["corners"]),
                len(record["odds_closing"]),
            )
        except Exception as exc:
            errors += 1
            log.error("%s failed: %s", match_id, exc)
        time.sleep(MATCH_DELAY_SEC)

    return done, errors


def scrape_pending_day(
    conn: Any,
    scraper: Scraper,
    iso_date: str,
    log: logging.Logger,
    *,
    output_jsonl: Path,
    json_only: bool = True,
    date_range: str | None = None,
) -> tuple[int, int]:
    """Scrape all pending/retryable matches on one calendar day."""
    rows = fetch_pending(conn, match_date=iso_date)
    if not rows:
        return 0, 0

    log.info("Day %s: scraping %d matches", iso_date, len(rows))
    done = 0
    errors = 0

    try:
        scraper.search_day(iso_date)
    except DaySearchError:
        log.warning(
            "Day %s: search failed after retries, keeping %d matches pending",
            iso_date,
            len(rows),
        )
        raise

    for row in rows:
        try:
            record = scrape_match(scraper, row, log, skip_search=True)
            upsert_record(
                record,
                jsonl_path=output_jsonl,
                date_range=date_range,
                json_only=json_only,
            )
            mark_done(conn, row["match_id"])
            done += 1
            log.info(
                "%s done corners=%s odds_sections=%d",
                row["match_id"],
                bool(record["corners"]),
                len(record["odds_closing"]),
            )
        except Exception as exc:
            errors += 1
            msg = str(exc)
            log.error("%s failed: %s", row["match_id"], msg)
            mark_error(conn, row["match_id"], msg)
        time.sleep(MATCH_DELAY_SEC)

    return done, errors


def run(args: argparse.Namespace) -> int:
    log = setup_logging()
    conn = connect(args.db)
    pending = fetch_pending(
        conn,
        match_id=args.match_id,
        match_date=args.date,
        start_date=args.start,
        end_date=args.end,
    )
    if not pending:
        summary = status_summary(conn)
        log.info("Nothing to scrape. Status: %s", summary)
        return 0

    log.info("Scraping %d matches", len(pending))

    from playwright.sync_api import sync_playwright

    done = 0
    errors = 0

    with sync_playwright() as p:
        if args.cdp:
            browser = p.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
        else:
            browser = p.chromium.launch(headless=not args.headed)
            context = browser.new_context(locale="zh-HK", viewport=VIEWPORT)
            page = context.new_page()

        scraper = Scraper(page, log)

        from itertools import groupby

        def date_key(row: Any) -> str:
            return row["match_date"]

        for iso_date, group in groupby(pending, key=date_key):
            rows = list(group)
            d, e = scrape_pending_day(
                conn,
                scraper,
                iso_date,
                log,
                output_jsonl=args.output_jsonl,
                json_only=args.json_only,
                date_range=args.date_range,
            )
            done += d
            errors += e

        if not args.cdp:
            browser.close()

    summary = status_summary(conn)
    log.info("Finished. done=%d errors=%d status=%s", done, errors, summary)
    conn.close()
    return 0 if errors == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="HKJC corners + closing odds scraper")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_RECORDS_JSON)
    parser.add_argument("--output-jsonl", type=Path, default=DEFAULT_RECORDS_JSONL)
    parser.add_argument("--match-id", help="Scrape single match id")
    parser.add_argument("--date", help="Scrape pending matches on YYYY-MM-DD")
    parser.add_argument("--start", help="Scrape pending matches from YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", help="Scrape pending matches to YYYY-MM-DD (inclusive)")
    parser.add_argument("--date-range", help="Label stored in records.json metadata")
    parser.add_argument("--json-only", action="store_true", help="Append JSONL only (for parallel workers)")
    parser.add_argument("--cdp", help="Optional CDP URL for debugging")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
