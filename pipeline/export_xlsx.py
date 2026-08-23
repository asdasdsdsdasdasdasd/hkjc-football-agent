#!/usr/bin/env python3
"""Export merged HKJC scrape records (records.json) to Excel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.config import DEFAULT_RECORDS_JSON, OUTPUT_DIR


def _corner_val(corners: dict[str, Any], period: str, field: str) -> int | None:
    block = corners.get(period) or {}
    val = block.get(field)
    return int(val) if val is not None else None


def match_rows(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in matches:
        scores = m.get("scores") or {}
        corners = m.get("corners") or {}
        rows.append(
            {
                "date": m.get("date"),
                "match_id": m.get("match_id"),
                "competition": m.get("competition"),
                "teams": m.get("teams"),
                "half_time_score": scores.get("half_time"),
                "full_time_score": scores.get("full_time"),
                "half_time_corners_total": _corner_val(corners, "half_time", "total"),
                "half_time_corners_home": _corner_val(corners, "half_time", "home"),
                "half_time_corners_away": _corner_val(corners, "half_time", "away"),
                "full_time_corners_total": _corner_val(corners, "full_time", "total"),
                "full_time_corners_home": _corner_val(corners, "full_time", "home"),
                "full_time_corners_away": _corner_val(corners, "full_time", "away"),
            }
        )
    return rows


def odds_rows(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in matches:
        base = {
            "date": m.get("date"),
            "match_id": m.get("match_id"),
            "competition": m.get("competition"),
            "teams": m.get("teams"),
        }
        odds_closing = m.get("odds_closing") or {}
        for section, entries in odds_closing.items():
            if not entries:
                continue
            for entry in entries:
                rows.append(
                    {
                        **base,
                        "section": section,
                        "selection": entry.get("selection"),
                        "line": entry.get("line"),
                        "odds": entry.get("odds"),
                        "over_odds": entry.get("over_odds"),
                        "under_odds": entry.get("under_odds"),
                    }
                )
    return rows


def _sort_by_date_desc(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["_sort_date"] = pd.to_datetime(out["date"], format="%d/%m/%Y", errors="coerce")
    sort_cols = ["_sort_date", "match_id"]
    extra = [c for c in ("section", "selection", "line") if c in out.columns]
    out = out.sort_values(sort_cols + extra, ascending=[False, True] + [True] * len(extra), na_position="last")
    return out.drop(columns=["_sort_date"]).reset_index(drop=True)


def export_xlsx(
    *,
    json_path: Path = DEFAULT_RECORDS_JSON,
    xlsx_path: Path | None = None,
) -> Path:
    xlsx_path = xlsx_path or (OUTPUT_DIR / "records.xlsx")

    with json_path.open(encoding="utf-8") as f:
        payload = json.load(f)

    matches: list[dict[str, Any]] = payload.get("matches") or []
    if not matches:
        raise SystemExit(f"No matches in {json_path}")

    df_matches = _sort_by_date_desc(pd.DataFrame(match_rows(matches)))
    df_odds = _sort_by_date_desc(pd.DataFrame(odds_rows(matches)))

    meta = pd.DataFrame(
        [
            {"field": "source", "value": str(json_path.resolve())},
            {"field": "date_range", "value": payload.get("date_range")},
            {"field": "recorded_at", "value": payload.get("recorded_at")},
            {"field": "match_count", "value": payload.get("match_count", len(matches))},
            {"field": "odds_row_count", "value": len(df_odds)},
        ]
    )

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        meta.to_excel(writer, sheet_name="meta", index=False)
        df_matches.to_excel(writer, sheet_name="matches", index=False)
        df_odds.to_excel(writer, sheet_name="odds", index=False)

    return xlsx_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export HKJC records.json to Excel")
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_RECORDS_JSON,
        help="Input records.json path",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .xlsx path (default: output/records.xlsx)",
    )
    args = parser.parse_args()

    out = export_xlsx(json_path=args.input, xlsx_path=args.output)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
