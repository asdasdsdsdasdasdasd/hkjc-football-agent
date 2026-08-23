#!/usr/bin/env python3
"""Forward predict for a calendar day.

Default policy is calibrated v5 (EV-based). Pass --policy v4 for the legacy
filter-wall path (kept for compatibility / audit).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.analyze_recommendations import _fmt_pick
from pipeline.betting.confidence import build_profile
from pipeline.betting.load import load_matches, parse_match_date
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.snapshots import latest_snapshot
from pipeline.evaluate_past_days_v3 import build_bet_only, cap_live, load_confidence_history
from pipeline.revise_recommendations import POLICY_VERSION as V4_POLICY_VERSION
from pipeline.revise_recommendations import revise

OUT = ROOT / "output"


def _as_date(value: str) -> date:
    value = value.strip()
    if "-" in value and value[4] == "-":
        return date.fromisoformat(value[:10])
    return parse_match_date(value)


def _run_v4(args: argparse.Namespace) -> int:
    iso = args.date
    tag = iso.replace("-", "")
    snapshot = args.snapshot or latest_snapshot(date=iso)
    if snapshot is None:
        snapshot = latest_snapshot(date=None)
    if snapshot is None or not Path(snapshot).exists():
        print("No odds snapshot found. Run snapshot_hkjc_odds_browser.js first.", file=sys.stderr)
        return 1

    print(f"policy={V4_POLICY_VERSION} date={iso} snapshot={snapshot}")
    targets = load_target_matches(Path(snapshot))
    target_day = _as_date(iso)
    day_targets = []
    for m in targets:
        try:
            md = _as_date(str(m.get("date") or ""))
        except Exception:
            continue
        if md == target_day:
            day_targets.append(m)
    if not day_targets:
        day_targets = targets
        print(f"warning: no exact date filter match; using all {len(targets)} snapshot matches")
    else:
        print(f"matches on {iso}: {len(day_targets)} (snapshot total {len(targets)})")

    features_map: dict[str, dict[str, float]] = {}
    if args.features and args.features.exists():
        features_map = load_feature_map(args.features)
        day_targets = merge_external_features(day_targets, features_map)
        print(f"features loaded: {len(features_map)} keys from {args.features}")
    else:
        print("warning: no features file — xG/weather gates inactive (document this)")

    play_styles = json.loads(args.styles.read_text(encoding="utf-8")) if args.styles.exists() else {}
    history = load_matches()

    bet_only = build_bet_only(day_targets, history)
    (OUT / f"tomorrow_{tag}_newmodel_bet_only.json").write_text(
        json.dumps(bet_only, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    revised_all, changes = revise(
        original_bets=bet_only,
        targets=day_targets,
        history=history,
        features_map=features_map,
        play_styles=play_styles,
    )
    for r in revised_all:
        r["pick"] = r.get("pick") or _fmt_pick(r)

    live = cap_live(revised_all, comp_min=args.comp_min, per_match=1)
    paper = [r for r in revised_all if r.get("bet") == "PAPER"]

    conf_hist = load_confidence_history(iso)
    profile = build_profile(conf_hist)
    live_ann = [{**r, **profile.annotate(r)} for r in live]
    paper_ann = [{**r, **profile.annotate(r)} for r in paper]

    live_path = OUT / f"tomorrow_{tag}_revised_bets.json"
    paper_path = OUT / f"tomorrow_{tag}_revised_bets_paper.json"
    live_path.write_text(json.dumps(live_ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paper_path.write_text(json.dumps(paper_ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / f"tomorrow_{tag}_revision_log.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "date": iso,
        "policy": V4_POLICY_VERSION,
        "snapshot": str(snapshot),
        "features": str(args.features) if args.features else None,
        "matches": len(day_targets),
        "newmodel_bet_only": len(bet_only),
        "live": len(live_ann),
        "paper": len(paper_ann),
        "by_confidence": {
            label: sum(1 for r in live_ann if r.get("confidence_label") == label)
            for label in ("HIGH", "MEDIUM", "UNPROVEN")
        },
    }
    (OUT / f"tomorrow_{tag}_v4_predict_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"-> {live_path}")
    print(f"-> {paper_path}")
    for r in live_ann:
        print(
            f"LIVE {r['match_id']} {r.get('teams','')} | {r['pick']} | "
            f"comp={r.get('composite_score')} old_p={r.get('old_p')} "
            f"conf={r.get('confidence_label')}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward predict (default: calibrated v5)")
    parser.add_argument("--date", required=True, help="Target match date YYYY-MM-DD")
    parser.add_argument("--policy", choices=("v5", "v4"), default="v5")
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--features", type=Path, default=None)
    parser.add_argument("--styles", type=Path, default=OUT / "play_styles_web_20260708.json")
    parser.add_argument("--comp-min", type=float, default=0.15)
    parser.add_argument("--min-ev", type=float, default=-0.05, help="v5 candidate floor")
    args = parser.parse_args(argv)

    if args.policy == "v4":
        return _run_v4(args)

    from pipeline.predict_forward_v5 import main as v5_main

    v5_argv = ["--date", args.date, "--min-ev", str(args.min_ev)]
    if args.snapshot is not None:
        v5_argv.extend(["--snapshot", str(args.snapshot)])
    if args.features is not None:
        v5_argv.extend(["--features", str(args.features)])
    return v5_main(v5_argv)


if __name__ == "__main__":
    raise SystemExit(main())
