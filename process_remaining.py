#!/usr/bin/env python3
"""
Process remaining HKJC matches using CDP (Chrome DevTools Protocol).
This runs directly on the browser.
"""
import json
import os
import re
import time

OUTPUT_FILE = "/mnt/d/openclaw/hkjc-football-agent/output/june-full-records.json"
MATCHES_TO_FIX = ['FB1079','FB1080','FB1084','FB1093','FB1102','FB1103','FB1108','FB1109']

# Load existing data
with open(OUTPUT_FILE) as f:
    data = json.load(f)

print(f"Loaded {len(data['matches'])} matches from {OUTPUT_FILE}")

def find_match(match_id):
    for i, m in enumerate(data['matches']):
        if m['match_id'] == match_id:
            return i, m
    return None, None

def save():
    data['recorded_at'] = time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime())
    data['match_count'] = len(data['matches'])
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(data['matches'])} matches")

# Print existing records for these matches
for mid in MATCHES_TO_FIX:
    idx, m = find_match(mid)
    if m:
        print(f"{mid}: teams={m.get('teams','?')}, has_corners={bool(m.get('corners',{}).get('full_time',{}).get('total'))}, has_odds={any(v for v in m.get('odds_closing',{}).values())}")
    else:
        print(f"{mid}: NOT FOUND in existing data")

print(f"\nMatches needing work: {[m for m in MATCHES_TO_FIX if not any(v for v in find_match(m)[1].get('odds_closing',{}).values()) if find_match(m)[1]]}")
