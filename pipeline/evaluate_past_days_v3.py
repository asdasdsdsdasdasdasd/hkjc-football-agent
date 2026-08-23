#!/usr/bin/env python3
"""Re-run v3 predictions on calendar days from records closing odds, then settle.

Same policy as the 07-11 forward run:
  - newmodel top3 with old_ev > 0.1
  - revise() with model-conflict / high-odds-goal-over / team-goal PAPER filters
  - live BET only, max 2/match, composite >= 0.15
  - annotate empirical confidence from prior settled days
"""

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
from pipeline.betting.confidence import build_profile
from pipeline.betting.load import load_matches, parse_match_date, parse_score_total
from pipeline.betting.markets import ALL_ADAPTERS, DEFAULT_MARKET_KEYS
from pipeline.betting.markets.team_ou import team_role_from_market
from pipeline.betting.models.external_features import load_feature_map, merge_external_features
from pipeline.betting.recommend import RecommendationConfig, recommend_bets
from pipeline.betting.settlement import pnl_over_under
from pipeline.betting.types import BetOpportunity, BetOutcome
from pipeline.pick_bet_direction import pick_directions
from pipeline.revise_recommendations import revise

OUT = ROOT / "output"


def _iter_days(start: str, end: str) -> list[str]:
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    days = []
    while cur <= last:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _load_records(iso: str) -> list[dict[str, Any]]:
    path = OUT / f"records-{iso}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _normalize_targets(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closing-odds records are already in the target match shape."""
    out = []
    for r in records:
        m = dict(r)
        # ensure date is parseable
        if "date" in m and "/" in str(m["date"]):
            # keep as-is; loaders accept DD/MM/YYYY
            pass
        out.append(m)
    return out


def settle(b: dict[str, Any], m: dict[str, Any] | None) -> tuple[str, float | None, str]:
    adapter = ALL_ADAPTERS.get(b["market"])
    if not m or not adapter:
        return "pending", None, "no record"
    opp = BetOpportunity(
        date=parse_match_date(m["date"]),
        match_id=b["match_id"],
        market=b["market"],
        line=b["line"],
        side=b["side"],
        decimal_odds=float(b["odds"]),
        teams=b.get("teams") or m.get("teams") or "",
        competition=b.get("competition") or m.get("competition"),
        over_odds=float(b["odds"]),
        under_odds=float(b["odds"]),
    )
    outcome = adapter.settle(opp, m)
    if outcome == BetOutcome.UNKNOWN:
        return "pending", None, "missing stat"
    role = team_role_from_market(b["market"])
    if role and b["market"].startswith("goal_ou"):
        period = "half_time" if b["market"].endswith("_ht") else "full_time"
        sc = (m.get("scores") or {}).get(period, "")
        if not sc or "無效" in str(sc):
            return "pending", None, f"no score {period}"
        parts = str(sc).replace(" ", "").split(":")
        total = int(parts[0]) if role == "home" and len(parts) == 2 else int(parts[1]) if len(parts) == 2 else None
    elif role and b["market"].startswith("corner_ou"):
        period = "half_time" if b["market"].endswith("_ht") else "full_time"
        block = (m.get("corners") or {}).get(period) or {}
        total = block.get(role)
        total = int(total) if total is not None else None
        if total is None:
            return "pending", None, "no corner data"
    elif b["market"].startswith("goal_ou"):
        period = "half_time" if b["market"].endswith("_ht") else "full_time"
        sc = (m.get("scores") or {}).get(period, "")
        if not sc or "無效" in str(sc):
            return "pending", None, f"no score {period}"
        total = parse_score_total(sc)
    else:
        period = "half_time" if b["market"].endswith("_ht") else "full_time"
        block = (m.get("corners") or {}).get(period) or {}
        total = block.get("total")
        total = int(total) if total is not None else None
        if total is None:
            return "pending", None, "no corner data"
    if total is None:
        return "pending", None, "missing total"
    _, pnl = pnl_over_under(total, b["line"], b["side"], float(b["odds"]), 1.0)
    return outcome.value, round(pnl, 4), f"{outcome.value.upper()} stat={total}"


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [r for r in rows if r.get("outcome") in ("win", "lose", "push")]
    w = sum(1 for r in settled if r["outcome"] == "win")
    l = sum(1 for r in settled if r["outcome"] == "lose")
    pnl = sum(float(r.get("pnl") or 0) for r in settled)
    n = len(settled)
    return {
        "bets": len(rows),
        "settled": n,
        "pending": len(rows) - n,
        "wins": w,
        "losses": l,
        "pnl": round(pnl, 2),
        "hit_pct": round(100 * w / (w + l), 1) if w + l else 0,
        "roi_pct": round(100 * pnl / n, 1) if n else 0,
    }


def build_bet_only(targets: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    market_rows = recommend_bets(
        history_matches=history,
        target_matches=targets,
        config=RecommendationConfig(
            market_keys=DEFAULT_MARKET_KEYS,
            min_ev=-999,
            min_edge=-999,
            min_train_matches=100,
            collapse_related=False,
        ),
    )
    old_rows = pick_directions(
        history_matches=history,
        target_matches=targets,
        market_keys=DEFAULT_MARKET_KEYS,
        min_train_matches=100,
    )
    old_by_key = {(r["match_id"], r["market"], r["line"]): r for r in old_rows}
    by_line: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in market_rows:
        by_line[(r.match_id, r.market, r.line)].append(r)

    all_top3 = []
    for (match_id, market, line), sides in by_line.items():
        old = old_by_key.get((match_id, market, line))
        if not old:
            continue
        chosen = next((s for s in sides if s.side == old["chosen_side"]), None)
        if not chosen:
            continue
        all_top3.append(
            {
                "date": chosen.date.isoformat(),
                "match_id": match_id,
                "teams": chosen.teams,
                "competition": chosen.competition or "",
                "market": market,
                "line": line,
                "side": chosen.side,
                "odds": round(chosen.odds, 4),
                "old_p": round(old["p_model_old"], 4),
                "old_ev": round(old["model_ev"], 4),
                "bet": "BET" if old["model_ev"] > 0.1 else "PASS",
            }
        )
    by_match: dict[str, list] = defaultdict(list)
    for row in all_top3:
        by_match[row["match_id"]].append(row)
    bet_only = []
    for mid in sorted(by_match):
        for r in sorted(by_match[mid], key=lambda x: (-float(x.get("old_ev", 0)), x["market"]))[:3]:
            if r["bet"] == "BET":
                bet_only.append(r)
    return bet_only


def cap_live(revised: list[dict[str, Any]], *, comp_min: float = 0.15, per_match: int = 1) -> list[dict[str, Any]]:
    """v4: live BET only, max 1/match (correlated lines inflated sample size)."""
    live = [
        r
        for r in revised
        if r.get("bet") == "BET"
        and float(r.get("composite_score") or 0) >= comp_min
        and r.get("side") == "under"
        and float(r.get("old_p") or 0) >= 0.80
        and float(r.get("odds") or 0) >= 1.40
        and "corner" not in str(r.get("market") or "")
        and r.get("action") in ("KEEP", "KEEP*", "FLIP", "UPGRADE", None)
    ]
    by_m: dict[str, list] = defaultdict(list)
    for r in live:
        by_m[r["match_id"]].append(r)
    capped = []
    for mid, rows in by_m.items():
        rows.sort(key=lambda x: -float(x.get("composite_score") or 0))
        capped.extend(rows[:per_match])
    return capped


def load_confidence_history(before_iso: str) -> list[dict[str, Any]]:
    """Settled rows strictly before the prediction day."""
    hist: list[dict[str, Any]] = []
    # prior multi-day eval reports
    for tag in ("20260704", "20260705", "20260706", "20260707"):
        p = OUT / f"tomorrow_{tag}_v3_eval_report.json"
        if p.exists():
            hist.extend(json.loads(p.read_text(encoding="utf-8")).get("lines_v3_v1_filter") or [])
    p910 = OUT / "tomorrow_20260709_10_bet_results.json"
    if p910.exists():
        hist.extend(json.loads(p910.read_text(encoding="utf-8")))
    # any previously generated settled backtests
    for p in sorted(OUT.glob("tomorrow_*_v3_day_bet_results.json")):
        hist.extend(json.loads(p.read_text(encoding="utf-8")))
    # keep only settled rows with date < before_iso
    cleaned = []
    for r in hist:
        d = str(r.get("date") or "")
        if len(d) >= 10 and d[:10] < before_iso and r.get("outcome") in ("win", "lose", "push"):
            cleaned.append(r)
    return cleaned


def evaluate_day(iso: str, history: list[dict[str, Any]], features_map: dict, play_styles: dict) -> dict[str, Any] | None:
    records = _load_records(iso)
    if not records:
        print(f"{iso}: no records, skip")
        return None
    targets = _normalize_targets(records)
    if features_map:
        targets = merge_external_features(targets, features_map)
    records_by_id = {r["match_id"]: r for r in records}

    bet_only = build_bet_only(targets, history)
    revised_all, changes = revise(
        original_bets=bet_only,
        targets=targets,
        history=history,
        features_map=features_map,
        play_styles=play_styles,
    )
    for r in revised_all:
        r["pick"] = r.get("pick") or _fmt_pick(r)

    capped = cap_live(revised_all)
    paper = [r for r in revised_all if r.get("bet") == "PAPER"]

    conf_hist = load_confidence_history(iso)
    profile = build_profile(conf_hist)
    annotated = [{**r, **profile.annotate(r)} for r in capped]
    paper_ann = [{**r, **profile.annotate(r)} for r in paper]

    rows = []
    for b in annotated:
        oc, pnl, detail = settle(b, records_by_id.get(b["match_id"]))
        rows.append({**b, "outcome": oc, "pnl": pnl, "detail": detail})

    paper_rows = []
    for b in paper_ann:
        oc, pnl, detail = settle(b, records_by_id.get(b["match_id"]))
        paper_rows.append({**b, "outcome": oc, "pnl": pnl, "detail": detail})

    tag = iso.replace("-", "")
    overall = stats(rows)
    by_label = {
        label: stats([r for r in rows if r.get("confidence_label") == label])
        for label in ("HIGH", "MEDIUM", "UNPROVEN")
    }
    report = {
        "date": iso,
        "mode": "closing_odds_backtest_v3",
        "matches": len(records),
        "corners_present": sum(1 for r in records if r.get("corners")),
        "newmodel_bet_only": len(bet_only),
        "live_capped": overall,
        "by_confidence": by_label,
        "paper": stats(paper_rows),
        "goal_only": stats([r for r in rows if "goal" in r["market"]]),
        "corner_only": stats([r for r in rows if "corner" in r["market"]]),
    }

    (OUT / f"tomorrow_{tag}_v3_day_bet_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / f"tomorrow_{tag}_v3_day_paper_results.json").write_text(
        json.dumps(paper_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / f"tomorrow_{tag}_v3_day_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / f"tomorrow_{tag}_revision_log.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"{iso}: matches={len(records)} live={overall['settled']} "
        f"{overall['wins']}W-{overall['losses']}L {overall['pnl']:+.2f}u "
        f"ROI {overall['roi_pct']:+.1f}% pend={overall['pending']} "
        f"MED={by_label['MEDIUM']['bets']} HIGH={by_label['HIGH']['bets']}"
    )
    return report


def settle_existing_forward(iso: str) -> dict[str, Any] | None:
    """Settle a previously saved forward prediction file against records."""
    tag = iso.replace("-", "")
    pred_path = OUT / f"tomorrow_{tag}_revised_bets.json"
    if not pred_path.exists():
        return None
    records = {r["match_id"]: r for r in _load_records(iso)}
    if not records:
        return None
    bets = json.loads(pred_path.read_text(encoding="utf-8"))
    rows = []
    for b in bets:
        oc, pnl, detail = settle(b, records.get(b["match_id"]))
        rows.append({**b, "outcome": oc, "pnl": pnl, "detail": detail})
    report = {
        "date": iso,
        "mode": "forward_snapshot_settlement",
        "source": str(pred_path),
        "live": stats(rows),
        "by_confidence": {
            label: stats([r for r in rows if r.get("confidence_label") == label])
            for label in ("HIGH", "MEDIUM", "UNPROVEN")
        },
        "goal_only": stats([r for r in rows if "goal" in r["market"]]),
        "corner_only": stats([r for r in rows if "corner" in r["market"]]),
    }
    (OUT / f"tomorrow_{tag}_forward_settlement.json").write_text(
        json.dumps({"report": report, "lines": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    s = report["live"]
    print(
        f"{iso} FORWARD: {s['wins']}W-{s['losses']}L {s['pnl']:+.2f}u "
        f"ROI {s['roi_pct']:+.1f}% pend={s['pending']}"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Past-day v3 predict + evaluate")
    parser.add_argument("--start", default="2026-07-11")
    parser.add_argument("--end", default="2026-07-22")
    parser.add_argument("--styles", type=Path, default=OUT / "play_styles_web_20260708.json")
    parser.add_argument("--features", type=Path, default=None, help="Optional BSD feature map JSON")
    args = parser.parse_args(argv)

    history = load_matches()
    play_styles = json.loads(args.styles.read_text(encoding="utf-8")) if args.styles.exists() else {}
    features_map: dict[str, dict[str, float]] = {}
    if args.features and args.features.exists():
        features_map = load_feature_map(args.features)

    days = _iter_days(args.start, args.end)
    day_reports = []
    forward_reports = []
    for iso in days:
        # Prefer evaluating existing forward files when present
        fr = settle_existing_forward(iso)
        if fr:
            forward_reports.append(fr)
        rep = evaluate_day(iso, history, features_map, play_styles)
        if rep:
            day_reports.append(rep)

    def _combine(reports: list[dict[str, Any]], key: str) -> dict[str, Any]:
        rows_key = key
        combined = {"wins": 0, "losses": 0, "pnl": 0.0, "settled": 0, "bets": 0, "pending": 0}
        for r in reports:
            s = r.get(rows_key) or {}
            combined["wins"] += s.get("wins", 0)
            combined["losses"] += s.get("losses", 0)
            combined["pnl"] += s.get("pnl", 0)
            combined["settled"] += s.get("settled", 0)
            combined["bets"] += s.get("bets", 0)
            combined["pending"] += s.get("pending", 0)
        n = combined["settled"]
        w, l = combined["wins"], combined["losses"]
        combined["pnl"] = round(combined["pnl"], 2)
        combined["hit_pct"] = round(100 * w / (w + l), 1) if w + l else 0
        combined["roi_pct"] = round(100 * combined["pnl"] / n, 1) if n else 0
        return combined

    summary = {
        "start": args.start,
        "end": args.end,
        "days_evaluated": [r["date"] for r in day_reports],
        "closing_odds_backtest": {
            "overall": _combine(day_reports, "live_capped"),
            "days": day_reports,
        },
        "forward_settlements": {
            "overall": _combine(
                [{"live_capped": r["live"], "date": r["date"]} for r in forward_reports],
                "live_capped",
            )
            if forward_reports
            else None,
            "days": forward_reports,
        },
    }
    out = OUT / f"eval_v3_{args.start.replace('-', '')}_{args.end.replace('-', '')}_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nCOMBINED closing-odds backtest:", summary["closing_odds_backtest"]["overall"])
    if summary["forward_settlements"]["overall"]:
        print("COMBINED forward settlements:", summary["forward_settlements"]["overall"])
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
