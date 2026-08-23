#!/usr/bin/env python3
"""Forward predict with calibrated v5 goal-total EV policy.

Live decisions come from calibrated expected value, not v4 hard filters:
  - markets: goal_ou_ft / goal_ou_ht (both sides)
  - LIVE when EV_calibrated > 0 and p_final > p_market (or market missing)
  - max 1 live bet per match
  - up to 3 LEAN rows (best positive-model / near-zero EV expressions)
  - corners / team goals stay out of the live book
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.analyze_recommendations import _fmt_pick
from pipeline.betting.confidence import build_profile
from pipeline.betting.load import load_matches, parse_match_date
from pipeline.betting.markets import get_adapters
from pipeline.betting.models.calibrated_total import MODEL_VERSION
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.recommend import RecommendationConfig, load_target_matches, recommend_bets
from pipeline.betting.snapshots import latest_snapshot
from pipeline.evaluate_past_days_v3 import load_confidence_history

OUT = ROOT / "output"
GOAL_MARKETS = ["goal_ou_ft", "goal_ou_ht"]
POLICY_VERSION = "v5"
MIN_LIVE_EV = 0.0
MIN_LIVE_EDGE = 0.0  # require p_final >= p_market
MAX_LIVE_PER_MATCH = 1
MAX_LEAN = 3


def _as_date(value: str) -> date:
    value = value.strip()
    if "-" in value and len(value) >= 10 and value[4] == "-":
        return date.fromisoformat(value[:10])
    return parse_match_date(value)


def _filter_day(targets: list[dict[str, Any]], iso: str) -> list[dict[str, Any]]:
    target_day = _as_date(iso)
    day = []
    for match in targets:
        try:
            if _as_date(str(match.get("date") or "")) == target_day:
                day.append(match)
        except Exception:
            continue
    return day


def _details_for(rec: Any, match: dict[str, Any], adapters: dict[str, Any]) -> dict[str, Any]:
    adapter = adapters.get(rec.market)
    if adapter is None or not hasattr(adapter, "predict_details"):
        return {
            "p_raw": rec.p_model,
            "p_market": rec.p_implied,
            "p_final": rec.p_model,
            "blend_weight": None,
            "calibration_n": None,
            "model_version": MODEL_VERSION,
        }
    # Re-fit is expensive; pull from recommend path's transient cache if present.
    cache = match.get("_v5_goal_diag") or {}
    key = (rec.market, rec.line, rec.side)
    if key in cache:
        return cache[key]
    return {
        "p_raw": rec.p_model,
        "p_market": rec.p_implied,
        "p_final": rec.p_model,
        "blend_weight": None,
        "calibration_n": None,
        "model_version": MODEL_VERSION,
    }


def score_day(
    *,
    history: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    min_ev: float = -0.05,
) -> list[dict[str, Any]]:
    adapters = {a.key: a for a in get_adapters(GOAL_MARKETS)}
    records = recommend_bets(
        history_matches=history,
        target_matches=targets,
        config=RecommendationConfig(
            market_keys=GOAL_MARKETS,
            min_ev=min_ev,
            min_edge=-1.0,
            min_train_matches=100,
            collapse_related=True,
            best_per_match=False,
        ),
    )
    targets_by_id = {m.get("match_id"): m for m in targets}
    # Rebuild fitted states so diagnostics are available even if cache was cold.
    state_by_market: dict[str, Any] = {}
    if targets:
        earliest = min(parse_match_date(m["date"]) for m in targets)
        train_pool = [m for m in history if parse_match_date(m["date"]) < earliest]
        for key, adapter in adapters.items():
            train = adapter.training_matches(train_pool)
            if len(train) >= 100:
                state_by_market[key] = adapter.fit(train)

    rows: list[dict[str, Any]] = []
    for rec in records:
        match = targets_by_id.get(rec.match_id) or {}
        adapter = adapters.get(rec.market)
        state = state_by_market.get(rec.market)
        details: dict[str, Any]
        if adapter is not None and state is not None and hasattr(adapter, "predict_details"):
            from pipeline.betting.types import BetOpportunity

            opp = BetOpportunity(
                date=rec.date,
                match_id=rec.match_id,
                market=rec.market,
                line=rec.line,
                side=rec.side,
                decimal_odds=rec.odds,
                teams=rec.teams,
                competition=rec.competition,
                over_odds=None,
                under_odds=None,
            )
            section = "半場入球大細" if rec.market == "goal_ou_ht" else "入球大細"
            for entry in ((match.get("odds_closing") or {}).get(section) or []):
                if str(entry.get("line")) == str(rec.line):
                    opp = BetOpportunity(
                        date=rec.date,
                        match_id=rec.match_id,
                        market=rec.market,
                        line=rec.line,
                        side=rec.side,
                        decimal_odds=rec.odds,
                        teams=rec.teams,
                        competition=rec.competition,
                        over_odds=float(entry["over_odds"]) if entry.get("over_odds") is not None else None,
                        under_odds=float(entry["under_odds"]) if entry.get("under_odds") is not None else None,
                    )
                    break
            details = adapter.predict_details(state, opp, match)
        else:
            details = _details_for(rec, match, adapters)

        p_final = float(details.get("p_final") or rec.p_model)
        p_market = details.get("p_market")
        if p_market is None:
            p_market = rec.p_implied
        edge = None if p_market is None else p_final - float(p_market)
        row = {
            "date": rec.date.isoformat(),
            "match_id": rec.match_id,
            "teams": rec.teams,
            "competition": rec.competition or "",
            "market": rec.market,
            "line": rec.line,
            "side": rec.side,
            "odds": round(rec.odds, 4),
            "p_raw": details.get("p_raw"),
            "p_market": round(float(p_market), 4) if p_market is not None else None,
            "p_final": round(p_final, 4),
            "edge_vs_market": round(edge, 4) if edge is not None else None,
            "ev": round(rec.ev, 4),
            "blend_weight": details.get("blend_weight"),
            "calibration_n": details.get("calibration_n"),
            "model_version": details.get("model_version") or MODEL_VERSION,
            "policy": POLICY_VERSION,
            "support_matches": rec.train_size,
            "pick": _fmt_pick(
                {"market": rec.market, "line": rec.line, "side": rec.side, "odds": rec.odds}
            ),
        }
        rows.append(row)
    rows.sort(key=lambda r: (-float(r.get("ev") or -999), r["match_id"], r["market"]))
    return rows


def select_live_and_lean(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    live_candidates = []
    for row in rows:
        ev = float(row.get("ev") or 0.0)
        edge = row.get("edge_vs_market")
        if ev <= MIN_LIVE_EV:
            continue
        if edge is not None and float(edge) < MIN_LIVE_EDGE:
            continue
        live_candidates.append(row)

    by_match: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live_candidates:
        by_match[row["match_id"]].append(row)

    live: list[dict[str, Any]] = []
    for mid, items in by_match.items():
        items.sort(key=lambda r: (-float(r.get("ev") or 0), -float(r.get("edge_vs_market") or 0)))
        chosen = dict(items[0])
        chosen["bet"] = "BET"
        chosen["action"] = "LIVE"
        live.append(chosen)
    live.sort(key=lambda r: (-float(r.get("ev") or 0), r["match_id"]))

    live_keys = {(r["match_id"], r["market"], r["line"], r["side"]) for r in live}
    lean: list[dict[str, Any]] = []
    for row in rows:
        key = (row["match_id"], row["market"], row["line"], row["side"])
        if key in live_keys:
            continue
        # LEAN: strongest remaining expressions with non-disastrous EV.
        if float(row.get("ev") or -1) < -0.02:
            continue
        item = dict(row)
        item["bet"] = "LEAN"
        item["action"] = "LEAN"
        lean.append(item)
        if len(lean) >= MAX_LEAN:
            break
    return live, lean


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Predict with calibrated v5 goal EV policy")
    parser.add_argument("--date", required=True, help="Target match date YYYY-MM-DD")
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--features", type=Path, default=None)
    parser.add_argument("--min-ev", type=float, default=-0.05, help="Candidate floor before LIVE/LEAN split")
    args = parser.parse_args(argv)

    iso = args.date
    tag = iso.replace("-", "")
    snapshot = args.snapshot or latest_snapshot(date=iso) or latest_snapshot(date=None)
    if snapshot is None or not Path(snapshot).exists():
        print("No odds snapshot found. Run snapshot_hkjc_odds_browser.js first.", file=sys.stderr)
        return 1

    print(f"policy={POLICY_VERSION} model={MODEL_VERSION} date={iso} snapshot={snapshot}")
    targets = load_target_matches(Path(snapshot))
    day_targets = _filter_day(targets, iso)
    if not day_targets:
        day_targets = targets
        print(f"warning: no exact date filter match; using all {len(targets)} snapshot matches")
    else:
        print(f"matches on {iso}: {len(day_targets)} (snapshot total {len(targets)})")

    if args.features and args.features.exists():
        day_targets = merge_external_features(day_targets, load_feature_map(args.features))
        print(f"features loaded from {args.features}")
    else:
        print("warning: no features file — opponent-aware model still runs; xG blend inactive")

    history = load_matches()
    scored = score_day(history=history, targets=day_targets, min_ev=args.min_ev)
    live, lean = select_live_and_lean(scored)

    profile = build_profile(load_confidence_history(iso))
    live_ann = [{**r, **profile.annotate(r)} for r in live]
    lean_ann = [{**r, **profile.annotate(r)} for r in lean]
    # Keep non-selected goal candidates as paper for audit.
    selected = {(r["match_id"], r["market"], r["line"], r["side"]) for r in live_ann + lean_ann}
    paper = []
    for row in scored:
        key = (row["match_id"], row["market"], row["line"], row["side"])
        if key in selected:
            continue
        item = dict(row)
        item["bet"] = "PAPER"
        item["action"] = "PAPER"
        item["paper_trade"] = True
        paper.append({**item, **profile.annotate(item)})

    live_path = OUT / f"tomorrow_{tag}_v5_live.json"
    lean_path = OUT / f"tomorrow_{tag}_v5_lean.json"
    paper_path = OUT / f"tomorrow_{tag}_v5_paper.json"
    # Compatibility: also write revised_bets as live-only for settlement helpers.
    compat_path = OUT / f"tomorrow_{tag}_revised_bets.json"

    live_path.write_text(json.dumps(live_ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lean_path.write_text(json.dumps(lean_ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paper_path.write_text(json.dumps(paper, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compat_path.write_text(json.dumps(live_ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "date": iso,
        "policy": POLICY_VERSION,
        "model_version": MODEL_VERSION,
        "snapshot": str(snapshot),
        "features": str(args.features) if args.features else None,
        "matches": len(day_targets),
        "candidates": len(scored),
        "live": len(live_ann),
        "lean": len(lean_ann),
        "paper": len(paper),
        "by_confidence": {
            label: sum(1 for r in live_ann if r.get("confidence_label") == label)
            for label in ("HIGH", "MEDIUM", "UNPROVEN")
        },
    }
    report_path = OUT / f"tomorrow_{tag}_v5_predict_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"-> {live_path}")
    print(f"-> {lean_path}")
    print(f"-> {paper_path}")
    for r in live_ann:
        print(
            f"LIVE {r['match_id']} {r.get('teams','')} | {r['pick']} | "
            f"ev={r.get('ev')} p_final={r.get('p_final')} edge={r.get('edge_vs_market')} "
            f"conf={r.get('confidence_label')}"
        )
    for r in lean_ann:
        print(
            f"LEAN {r['match_id']} {r.get('teams','')} | {r['pick']} | "
            f"ev={r.get('ev')} p_final={r.get('p_final')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
