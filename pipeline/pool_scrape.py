#!/usr/bin/env python3
"""Day-pool scraper: N workers each claim one calendar day at a time."""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import (  # noqa: E402
    DAY_SEARCH_FAIL_BACKOFF_SEC,
    DEFAULT_DB,
    DEFAULT_POOL_WORKERS,
    OUTPUT_DIR,
)
from pipeline.db import (  # noqa: E402
    claim_next_pending_day,
    clear_day_locks,
    connect,
    pending_day_count,
    release_day_lock,
    reset_date_search_errors,
    status_summary,
)
from pipeline.parsers import format_date_dmy  # noqa: E402
from pipeline.scrape import (  # noqa: E402
    DaySearchError,
    Scraper,
    scrape_pending_day,
    setup_logging,
)

VIEWPORT = {"width": 1400, "height": 1200}


def worker_loop(worker_id: int, args: argparse.Namespace) -> int:
    log = setup_logging(worker_id)
    conn = connect(args.db)
    worker_label = f"w{worker_id:02d}"
    total_done = 0
    total_errors = 0
    days_done = 0

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="zh-HK", viewport=VIEWPORT)
        page = context.new_page()
        scraper = Scraper(page, log)

        while True:
            day = claim_next_pending_day(
                conn, worker_label, args.start, args.end
            )
            if not day:
                break

            jsonl = OUTPUT_DIR / f"records-{day}.jsonl"
            range_label = format_date_dmy(day)
            log.info("Claimed %s -> %s", day, jsonl.name)
            try:
                done, errors = scrape_pending_day(
                    conn,
                    scraper,
                    day,
                    log,
                    output_jsonl=jsonl,
                    json_only=True,
                    date_range=range_label,
                )
                total_done += done
                total_errors += errors
                days_done += 1
                log.info(
                    "Finished %s: matches done=%d errors=%d",
                    day,
                    done,
                    errors,
                )
            except DaySearchError as exc:
                log.warning(
                    "Day %s skipped (search failed), backoff %.0fs",
                    exc.iso_date,
                    DAY_SEARCH_FAIL_BACKOFF_SEC,
                )
                time.sleep(DAY_SEARCH_FAIL_BACKOFF_SEC)
            finally:
                release_day_lock(conn, day)

        browser.close()

    summary = status_summary(conn)
    log.info(
        "Worker %s exit: days=%d matches done=%d errors=%d status=%s",
        worker_label,
        days_done,
        total_done,
        total_errors,
        summary,
    )
    conn.close()
    return 0 if total_errors == 0 else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HKJC day-pool scraper (N workers, one day each)"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--workers", type=int, default=DEFAULT_POOL_WORKERS)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 1

    conn = connect(args.db)
    clear_day_locks(conn)
    reset = reset_date_search_errors(conn)
    if reset:
        print(f"    Reset {reset} date-search errors -> pending")
    days_left = pending_day_count(conn, args.start, args.end)
    summary = status_summary(conn)
    conn.close()

    print(
        f"==> Pool scrape {args.start} .. {args.end}: "
        f"{args.workers} workers, {days_left} days with pending work"
    )
    print(f"    DB status: {summary}")

    if days_left == 0:
        print("Nothing to scrape.")
        return 0

    ctx = mp.get_context("spawn")
    procs: list[mp.Process] = []
    for i in range(1, args.workers + 1):
        p = ctx.Process(target=worker_loop, args=(i, args), name=f"pool-{i:02d}")
        p.start()
        procs.append(p)
        time.sleep(2.0)

    fail = 0
    for p in procs:
        p.join()
        if p.exitcode not in (0, None):
            fail += 1

    conn = connect(args.db)
    clear_day_locks(conn)
    summary = status_summary(conn)
    remaining = pending_day_count(conn, args.start, args.end)
    conn.close()

    print(f"==> Pool finished. workers_failed={fail} status={summary}")
    print(f"    days still pending: {remaining}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
