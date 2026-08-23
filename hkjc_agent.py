#!/usr/bin/env python3
"""HKJC Football Agent - helper for incremental JSON saves."""
import json, os, sys
from datetime import datetime

OUTPUT = "/mnt/d/openclaw/hkjc-football-agent/output/june-full-records.json"

def load_existing():
    if os.path.exists(OUTPUT):
        with open(OUTPUT) as f:
            return json.load(f)
    return {"date_range": "01/06/2026 - 30/06/2026", "recorded_at": "", "match_count": 0, "matches": []}

def save(data):
    data["match_count"] = len(data["matches"])
    data["recorded_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def upsert_match(match):
    data = load_existing()
    # Remove existing entry for this match_id
    data["matches"] = [m for m in data["matches"] if m["match_id"] != match["match_id"]]
    data["matches"].append(match)
    # Sort by match_id
    data["matches"].sort(key=lambda m: m["match_id"])
    save(data)
    return data["match_count"]

def get_existing_ids():
    data = load_existing()
    return {m["match_id"] for m in data["matches"]}

def show_progress():
    data = load_existing()
    print(f"Total matches saved: {data['match_count']}")
    print(f"Match IDs: {', '.join(m['match_id'] for m in data['matches'])}")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'show':
        show_progress()
    elif len(sys.argv) > 1 and sys.argv[1] == 'ids':
        ids = get_existing_ids()
        for i in sorted(ids):
            print(i)
    else:
        print(f"Usage: {sys.argv[0]} show|ids")
