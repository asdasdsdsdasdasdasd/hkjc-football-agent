#!/usr/bin/env python3
"""Re-scrape calendar days from the HKJC results table (handles recycled match IDs)."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import OUTPUT_DIR  # noqa: E402
from pipeline.parsers import format_date_dmy  # noqa: E402
from pipeline.scrape import Scraper, scrape_calendar_day, setup_logging  # noqa: E402

VIEWPORT = {"width": 1400, "height": 1200}


def iter_days(start: str, end: str):
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while cur <= last:
        yield cur.isoformat()
        cur += timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape HKJC results by calendar day from the web table"
    )
    parser.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--skip-corners",
        action="store_true",
        help="Skip 足智資料庫 corner scrape (faster; goal markets still settle)",
    )
    args = parser.parse_args()

    log = setup_logging()
    total_done = 0
    total_errors = 0

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="zh-HK", viewport=VIEWPORT)
        page = context.new_page()
        scraper = Scraper(page, log)

        for iso_date in iter_days(args.start, args.end):
            jsonl = OUTPUT_DIR / f"records-{iso_date}.jsonl"
            range_label = format_date_dmy(iso_date)
            log.info("=== Calendar retry %s -> %s ===", iso_date, jsonl.name)
            done, errors = scrape_calendar_day(
                scraper,
                iso_date,
                log,
                output_jsonl=jsonl,
                date_range=range_label,
                skip_corners=args.skip_corners,
            )
            total_done += done
            total_errors += errors
            log.info("Day %s finished: done=%d errors=%d", iso_date, done, errors)
            time.sleep(2.0)

        browser.close()

    log.info("Calendar retry complete: done=%d errors=%d", total_done, total_errors)
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
