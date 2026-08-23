#!/usr/bin/env python3
"""Merge worker JSONL shards into records.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import DEFAULT_RECORDS_JSON, OUTPUT_DIR  # noqa: E402
from pipeline.storage import merge_jsonl_files  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge HKJC pipeline JSONL shards")
    parser.add_argument("--output", type=Path, default=DEFAULT_RECORDS_JSON)
    parser.add_argument("--date-range", help="Metadata label for merged records.json")
    parser.add_argument(
        "--glob",
        default="records-*.jsonl",
        help="Glob under output/ for worker shards (default: records-*.jsonl)",
    )
    parser.add_argument("jsonl", nargs="*", type=Path, help="Explicit JSONL files to merge")
    args = parser.parse_args()

    if args.jsonl:
        paths = args.jsonl
    else:
        paths = sorted(OUTPUT_DIR.glob(args.glob))

    if not paths:
        print("No JSONL shards found.", file=sys.stderr)
        raise SystemExit(1)

    payload = merge_jsonl_files(paths, json_path=args.output, date_range=args.date_range)
    print(f"Merged {len(paths)} files -> {args.output} ({payload['match_count']} matches)")


if __name__ == "__main__":
    main()
