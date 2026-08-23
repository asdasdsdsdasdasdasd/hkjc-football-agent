#!/usr/bin/env python3
"""Merge externally supplied match features into a match JSON file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.recommend import load_target_matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach xG/shots/injury/lineup/weather features to match JSON")
    parser.add_argument("--input", type=Path, required=True, help="Match JSON, list, or {matches:[...]} file")
    parser.add_argument("--features", type=Path, required=True, help="Feature JSON file keyed by match_id or date+teams")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = parser.parse_args()

    matches = merge_external_features(load_target_matches(args.input), load_feature_map(args.features))
    payload = {"matches": matches}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} matches={len(matches)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
