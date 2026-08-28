#!/usr/bin/env python3
"""Generate the v3.2 live book for one calendar day.

v3.2 live cut (pipeline.eval_recent_models.cap_live_v31):
  - HT goal under, or weekday FT corner under (line >= 10.5)
  - odds 1.55–1.80, composite 0.10–0.50
  - max 1 bet / match
  - Saturday/Sunday: no corner FT unders
  - composite is market-aware (revise_recommendations.MARKET_AWARE_COMPOSITE)

Example:
  PYTHONPATH=. python3 predict_v32.py --date 2026-08-24 \\
      --snapshot output/odds_snapshots/hkjc-browser-ch-all-2026-08-24.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

ISO = ""
SNAPSHOT = Path()
STYLES = ROOT / "output" / "play_styles_web_20260708.json"
OUT_DIR = ROOT / "output"


def _parse_day(m: dict) -> date | None:
    from pipeline.betting.load import parse_match_date

    raw = str(m.get("date") or "")
    try:
        if "-" in raw and len(raw) >= 10 and raw[4] == "-":
            return date.fromisoformat(raw[:10])
        return parse_match_date(raw)
    except Exception:
        return None


def _targets() -> list[dict]:
    from pipeline.betting.recommend import load_target_matches

    td = date.fromisoformat(ISO)
    kwargs = {}
    db = ROOT / "data" / "pipeline.db"
    if db.exists():
        kwargs["db_path"] = db
    all_m = load_target_matches(SNAPSHOT, **kwargs)
    return [m for m in all_m if _parse_day(m) == td]


def _revise_one(match_id: str) -> list[dict]:
    from pipeline.analyze_recommendations import _fmt_pick
    from pipeline.betting.load import load_matches, parse_match_date
    from pipeline.evaluate_past_days_v3 import build_bet_only
    from pipeline.revise_recommendations import revise

    match = next((m for m in _targets() if m.get("match_id") == match_id), None)
    if match is None:
        return []
    cutoff = date.fromisoformat(ISO)
    history = [m for m in load_matches() if parse_match_date(m["date"]) < cutoff]
    styles = json.loads(STYLES.read_text(encoding="utf-8")) if STYLES.exists() else {}
    bet_only = build_bet_only([match], history)
    revised, _ = revise(
        original_bets=bet_only,
        targets=[match],
        history=history,
        features_map={},
        play_styles=styles,
        match_ids={match_id},
        policy="v3.1",
    )
    for r in revised:
        r["pick"] = r.get("pick") or _fmt_pick(r)
        r["date"] = ISO
    return revised


def main() -> int:
    global ISO, SNAPSHOT

    from pipeline.analyze_recommendations import _fmt_pick
    from pipeline.eval_recent_models import cap_live_v31

    ap = argparse.ArgumentParser(description="v3.2 live book for one day")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 4))
    args = ap.parse_args()

    ISO = args.date
    SNAPSHOT = args.snapshot.resolve()
    if not SNAPSHOT.exists():
        print(f"snapshot not found: {SNAPSHOT}", file=sys.stderr)
        return 2

    targets = _targets()
    ids = [m["match_id"] for m in targets if m.get("match_id")]
    print(f"{ISO}: {len(ids)} matches, workers={args.jobs}")
    if not ids:
        return 3

    revised: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(_revise_one, mid): mid for mid in ids}
        for i, fut in enumerate(as_completed(futs), 1):
            revised.extend(fut.result())
            if i % 10 == 0 or i == len(ids):
                print(f"  revised {i}/{len(ids)}", flush=True)

    for r in revised:
        r["pick"] = r.get("pick") or _fmt_pick(r)

    live = sorted(cap_live_v31(revised), key=lambda r: -float(r.get("composite_score") or 0))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    tag = ISO.replace("-", "")
    revised_out = args.out_dir / f"tomorrow_{tag}_v32_revised.json"
    revised_out.write_text(json.dumps(revised, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"revised ({len(revised)}) -> {revised_out}")
    out = args.out_dir / f"tomorrow_{tag}_v32_live.json"
    out.write_text(json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nv3.2 LIVE ({len(live)}) -> {out}")
    for r in live:
        print(
            f"LIVE {r['match_id']} {r.get('teams')} | {r.get('pick')} | "
            f"odds={r.get('odds')} comp={r.get('composite_score')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
