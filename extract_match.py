#!/usr/bin/env python3
"""Extract odds data from FB0120 detail panel and save to june-full-records.json"""
import json, os, sys, re
from datetime import datetime

OUTPUT = "/mnt/d/openclaw/hkjc-football-agent/output/june-full-records.json"

def load_data():
    if os.path.exists(OUTPUT):
        with open(OUTPUT) as f:
            return json.load(f)
    return {"date_range": "01/06/2026 - 30/06/2026", "recorded_at": "", "match_count": 0, "matches": []}

def save_data(data):
    data["match_count"] = len(data["matches"])
    data["recorded_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"SAVED: {data['match_count']} matches")

def save_match(match):
    data = load_data()
    data["matches"] = [m for m in data["matches"] if m["match_id"] != match["match_id"]]
    data["matches"].append(match)
    data["matches"].sort(key=lambda m: m["match_id"])
    save_data(data)
    print(f"MATCH OK: {match['match_id']} - {match['teams']}")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'save':
        match = json.loads(sys.argv[2])
        save_match(match)
    else:
        print("Usage: extract_match.py save '<json>'")
