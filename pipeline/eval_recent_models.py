#!/usr/bin/env python3
"""Settle forward files and closing-odds backtests for v3 / v3.1 / v4 / v5."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.analyze_recommendations import _fmt_pick
from pipeline.betting.load import load_matches
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.evaluate_past_days_v3 import (
    cap_live,
    settle,
    stats,
)
from pipeline.predict_forward_v5 import score_day, select_live_and_lean
from pipeline.revise_recommendations import _line_float, revise

OUT = ROOT / "output"


def _iter_days(start: str, end: str) -> list[str]:
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    days = []
    while cur <= last:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def load_day_records(iso: str) -> list[dict[str, Any]]:
    path = OUT / f"records-{iso}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def cap_live_v3(
    revised: list[dict[str, Any]],
    *,
    comp_min: float = 0.15,
    comp_gt: float | None = None,
    per_match: int = 2,
) -> list[dict[str, Any]]:
    """Original v3 live cut: BET, composite floor, max N/match, overs+corners allowed."""
    live = []
    for row in revised:
        market = str(row.get("market") or "")
        if "goal_ou_home" in market or "goal_ou_away" in market:
            continue
        bet = row.get("bet")
        is_corner = "corner" in market
        # v3 revise emits corners as BET; v4 revise papers them — both are live here.
        if bet == "BET":
            pass
        elif is_corner and bet == "PAPER":
            pass
        else:
            continue
        if row.get("action") not in ("KEEP", "KEEP*", "FLIP", "UPGRADE", "NEW", "PAPER", None):
            continue
        comp = float(row.get("composite_score") or 0)
        if comp_gt is not None:
            if not (comp > comp_gt):
                continue
        elif comp < comp_min:
            continue
        live.append(row)
    by_match: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live:
        by_match[row["match_id"]].append(row)
    capped: list[dict[str, Any]] = []
    for rows in by_match.values():
        rows.sort(key=lambda x: -float(x.get("composite_score") or 0))
        capped.extend(rows[:per_match])
    return capped


def _iso_weekday(raw: Any) -> int | None:
    """Weekday from an ISO date/datetime string. Monday=0 … Sunday=6."""
    text = str(raw or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10]).weekday()
    except ValueError:
        return None


def cap_live_v31(revised: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """v3.2 live cut: HT goal under + weekday FT corner under (line>=10.5).

    Odds 1.55–1.80, composite 0.10–0.50, max 1 bet/match (highest composite).

    2026-08-22: raised the odds floor 1.40→1.55.  The 08-14..21 live window
    showed price is the dominant filter: odds<=1.55 ate the thin under edge;
    odds>1.60 stayed positive.  Composite is now market-aware (see
    revise_recommendations.MARKET_AWARE_COMPOSITE).
    """
    live: list[dict[str, Any]] = []
    for row in revised:
        if row.get("bet") not in ("BET", "BET*"):
            continue
        market = str(row.get("market") or "")
        side = row.get("side")
        is_goal_ht_under = market == "goal_ou_ht" and side == "under"
        is_corner_ft_under = market == "corner_ou_ft" and side == "under"
        if not (is_goal_ht_under or is_corner_ft_under):
            continue
        odds = float(row.get("odds") or 0)
        if not (1.55 <= odds <= 1.80):
            continue
        comp = float(row.get("composite_score") or 0)
        if not (0.10 <= comp <= 0.50):
            continue
        if is_corner_ft_under:
            if _line_float(str(row.get("line") or "")) < 10.5:
                continue
            weekday = _iso_weekday(row.get("date"))
            if weekday in (5, 6):  # Saturday / Sunday
                continue
        live.append(row)
    by_match: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live:
        by_match[row["match_id"]].append(row)
    capped: list[dict[str, Any]] = []
    for rows in by_match.values():
        rows.sort(key=lambda x: -float(x.get("composite_score") or 0))
        capped.extend(rows[:1])
    return capped


def settle_bets(bets: list[dict[str, Any]], records_by_id: dict[str, dict[str, Any]], policy: str, iso: str) -> list[dict[str, Any]]:
    rows = []
    for bet in bets:
        match = records_by_id.get(bet["match_id"])
        outcome, pnl, detail = settle(bet, match)
        note = detail
        if match:
            scores = match.get("scores") or {}
            corners = match.get("corners") or {}
            note = f"scores FT={scores.get('full_time')} HT={scores.get('half_time')} | {detail}"
            if "corner" in str(bet.get("market") or ""):
                note += (
                    f" | corners FT={(corners.get('full_time') or {}).get('total')} "
                    f"HT={(corners.get('half_time') or {}).get('total')}"
                )
        pick = bet.get("pick") or _fmt_pick(bet)
        rows.append(
            {
                "policy": policy,
                "date": iso,
                "match_id": bet["match_id"],
                "teams": bet.get("teams") or (match.get("teams") if match else ""),
                "market": bet.get("market"),
                "pick": pick,
                "odds": bet.get("odds"),
                "side": bet.get("side"),
                "composite_score": bet.get("composite_score"),
                "old_p": bet.get("old_p"),
                "p_final": bet.get("p_final"),
                "ev": bet.get("ev") or bet.get("revised_ev") or bet.get("old_ev"),
                "outcome": outcome,
                "pnl": pnl,
                "note": note,
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    goal = [r for r in rows if "goal" in str(r.get("market") or "")]
    corner = [r for r in rows if "corner" in str(r.get("market") or "")]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[str(row.get("date") or "")].append(row)
    return {
        "bets": len(rows),
        "overall": stats(rows),
        "goal_only": stats(goal),
        "corner_only": stats(corner),
        "over": stats([r for r in rows if r.get("side") == "over"]),
        "under": stats([r for r in rows if r.get("side") == "under"]),
        "by_day": {d: stats(rs) for d, rs in sorted(by_day.items())},
    }


def run_closing_odds_day(
    iso: str,
    history: list[dict[str, Any]],
    features_map: dict[str, dict[str, float]],
    play_styles: dict,
    policies: set[str],
) -> dict[str, list[dict[str, Any]]]:
    from pipeline.evaluate_past_days_v3 import build_bet_only

    records = load_day_records(iso)
    if not records:
        return {}
    targets = list(records)
    if features_map:
        targets = merge_external_features(targets, features_map)
    records_by_id = {r["match_id"]: r for r in records}

    bet_only = build_bet_only(targets, history)
    out: dict[str, list[dict[str, Any]]] = {}

    if "v3" in policies:
        revised_v3, _changes = revise(
            original_bets=bet_only,
            targets=targets,
            history=history,
            features_map=features_map,
            play_styles=play_styles,
            policy="v3",
        )
        for row in revised_v3:
            row["pick"] = row.get("pick") or _fmt_pick(row)
        v3 = cap_live_v3(revised_v3, comp_min=0.15, per_match=2)
        v3_strict = cap_live_v3(revised_v3, comp_gt=0.5, per_match=2)
        out["v3"] = settle_bets(v3, records_by_id, "v3-closing", iso)
        out["v3_comp_gt_0.5"] = settle_bets(v3_strict, records_by_id, "v3-closing-comp>0.5", iso)

    if "v3.1" in policies:
        revised_v31, _changes = revise(
            original_bets=bet_only,
            targets=targets,
            history=history,
            features_map=features_map,
            play_styles=play_styles,
            policy="v3.1",
        )
        for row in revised_v31:
            row["pick"] = row.get("pick") or _fmt_pick(row)
        v31 = cap_live_v31(revised_v31)
        out["v3.1"] = settle_bets(v31, records_by_id, "v3.1-closing", iso)

    if "v4" in policies:
        revised_v4, _changes = revise(
            original_bets=bet_only,
            targets=targets,
            history=history,
            features_map=features_map,
            play_styles=play_styles,
            policy="v4",
        )
        for row in revised_v4:
            row["pick"] = row.get("pick") or _fmt_pick(row)
        v4 = cap_live(revised_v4, comp_min=0.15, per_match=1)
        out["v4"] = settle_bets(v4, records_by_id, "v4-closing", iso)

    if "v5" in policies:
        v5_scored = score_day(history=history, targets=targets, min_ev=-0.05)
        v5_live, _lean = select_live_and_lean(v5_scored)
        out["v5"] = settle_bets(v5_live, records_by_id, "v5-closing", iso)

    return out


def settle_forward_day(iso: str) -> dict[str, list[dict[str, Any]]]:
    records_by_id = {r["match_id"]: r for r in load_day_records(iso)}
    if not records_by_id:
        return {}
    tag = iso.replace("-", "")
    out: dict[str, list[dict[str, Any]]] = {}
    mapping = {
        "v5": OUT / f"tomorrow_{tag}_v5_live.json",
        "v4": OUT / f"tomorrow_{tag}_revised_bets.json",
        "v3": OUT / f"tomorrow_{tag}_v3_replay_live.json",
    }
    # 07-24 v4 was overwritten by v5 compat writer.
    if iso == "2026-07-24":
        v4_0724 = [
            {
                "match_id": "FB1732",
                "teams": "普雷斯頓雄獅 對 賓特利綠軍",
                "market": "goal_ou_ft",
                "line": "[3.5]",
                "side": "under",
                "odds": 1.52,
                "old_p": 0.8331,
            }
        ]
        out["v4"] = settle_bets(v4_0724, records_by_id, "v4-forward", iso)
    for policy, path in mapping.items():
        if policy == "v4" and iso == "2026-07-24":
            continue
        bets = load_json_list(path)
        if not bets:
            continue
        # If revised_bets is a v5 copy, skip as v4.
        if policy == "v4":
            v5 = load_json_list(OUT / f"tomorrow_{tag}_v5_live.json")
            v5_keys = {(b["match_id"], b.get("market"), b.get("side"), str(b.get("line"))) for b in v5}
            v4_keys = {(b["match_id"], b.get("market"), b.get("side"), str(b.get("line"))) for b in bets}
            if v5 and v4_keys == v5_keys:
                continue
        out[policy] = settle_bets(bets, records_by_id, f"{policy}-forward", iso)
        if policy == "v3":
            strict = [b for b in bets if float(b.get("composite_score") or 0) > 0.5]
            out["v3_comp_gt_0.5"] = settle_bets(strict, records_by_id, "v3-forward-comp>0.5", iso)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eval recent v3/v4/v5 performance")
    parser.add_argument("--start", default="2026-07-24")
    parser.add_argument("--end", default="2026-08-13")
    parser.add_argument("--styles", type=Path, default=OUT / "play_styles_web_20260708.json")
    parser.add_argument("--features", type=Path, default=None)
    parser.add_argument("--skip-closing", action="store_true")
    parser.add_argument(
        "--policies",
        default="v3,v4,v5",
        help="Comma-separated closing policies: v3,v3.1,v4,v5",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    policies = {p.strip() for p in args.policies.split(",") if p.strip()}

    days = _iter_days(args.start, args.end)
    play_styles = json.loads(args.styles.read_text(encoding="utf-8")) if args.styles.exists() else {}
    features_map: dict[str, dict[str, float]] = {}
    if args.features and args.features.exists():
        features_map = load_feature_map(args.features)
    history = load_matches()

    coverage = {}
    forward_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    closing_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for iso in days:
        recs = load_day_records(iso)
        n = len(recs)
        wc = 0
        for rec in recs:
            corners = rec.get("corners") or {}
            if (corners.get("full_time") or {}).get("total") is not None:
                wc += 1
        coverage[iso] = {"matches": n, "with_corners": wc}
        if not recs:
            continue
        fwd = settle_forward_day(iso)
        for key, rows in fwd.items():
            forward_rows[key].extend(rows)
        if not args.skip_closing:
            print(f"{iso}: closing-odds backtest ({n} matches, {wc} with corners) policies={sorted(policies)}")
            closed = run_closing_odds_day(iso, history, features_map, play_styles, policies)
            for key, rows in closed.items():
                closing_rows[key].extend(rows)

    report = {
        "window": f"{args.start} .. {args.end}",
        "caveats": [
            "Forward files only exist for days previously predicted (mainly 07-24..08-02).",
            "Closing-odds backtest uses records closing prices (optimistic vs true board odds).",
            "v3 live cut: BET (incl. corners/overs), composite>=0.15, max 2/match; strict slice is composite>0.5.",
            "v3.2 live cut: goal HT under or weekday corner FT under (line>=10.5), odds 1.55-1.80, composite 0.10-0.50, max 1/match.",
            "v4 live cut: under only, old_p>=0.80, odds>=1.40, no corners, max 1/match.",
            "v5 live: calibrated EV>0 and edge>=0, goal totals only, max 1/match.",
        ],
        "records_coverage": coverage,
        "forward": {k: summarize(v) for k, v in sorted(forward_rows.items())},
        "closing_odds": {k: summarize(v) for k, v in sorted(closing_rows.items())},
        "forward_detail": {k: v for k, v in sorted(forward_rows.items())},
        "closing_detail": {k: v for k, v in sorted(closing_rows.items())},
    }
    out = args.out or OUT / f"eval_recent_{args.start.replace('-', '')}_{args.end.replace('-', '')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def show(title: str, block: dict[str, Any]) -> None:
        print(f"\n=== {title} ===")
        if not block:
            print("  (none)")
            return
        for name, summary in block.items():
            o = summary["overall"]
            g = summary["goal_only"]
            c = summary["corner_only"]
            print(
                f"{name}: settled={o['settled']}/{summary['bets']} "
                f"W-L={o['wins']}-{o['losses']} pnl={o['pnl']:+.2f}u "
                f"ROI={o['roi_pct']:+.1f}% pend={o['pending']}"
            )
            print(
                f"       goal {g['settled']} {g['wins']}-{g['losses']} {g['pnl']:+.2f}u "
                f"| corner {c['settled']} {c['wins']}-{c['losses']} {c['pnl']:+.2f}u"
            )

    print("Records:", {d: f"{v['matches']}m/{v['with_corners']}c" for d, v in coverage.items()})
    show("FORWARD (true snapshots)", report["forward"])
    show("CLOSING-ODDS BACKTEST", report["closing_odds"])
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
