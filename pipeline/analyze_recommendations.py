#!/usr/bin/env python3
"""Form + play-style analysis for recommended bets."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.betting.load import load_matches, parse_match_date, split_teams
from pipeline.betting.markets import get_adapters
from pipeline.betting.markets.team_ou import team_role_from_market
from pipeline.betting.models.poisson_total import fit_poisson_total, predict_match_mu, predict_side_probability, predict_team_mu
from pipeline.betting.recent_form import match_form_summary
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.snapshots import latest_snapshot
from pipeline.pick_bet_direction import pick_directions


def _fmt_pick(row: dict[str, Any]) -> str:
    market = row["market"]
    m = (
        market.replace("match_1x2_ht", "HT 1X2")
        .replace("match_1x2", "1X2")
        .replace("goal_ou_home_ft", "home goal FT")
        .replace("goal_ou_away_ft", "away goal FT")
        .replace("goal_ou_home_ht", "home goal HT")
        .replace("goal_ou_away_ht", "away goal HT")
        .replace("corner_ou_home_ft", "home corner FT")
        .replace("corner_ou_away_ft", "away corner FT")
        .replace("corner_ou_home_ht", "home corner HT")
        .replace("corner_ou_away_ht", "away corner HT")
        .replace("goal_ou_ft", "goal FT")
        .replace("goal_ou_ht", "goal HT")
        .replace("corner_ou_ft", "corner FT")
        .replace("corner_ou_ht", "corner HT")
    )
    return f"{m} {row['line']} {row['side']} @ {row['odds']}"


def _model_mu_for_bet(match: dict[str, Any], market: str, train: list[dict[str, Any]]) -> float | None:
    if "corner" not in market and "goal" not in market:
        return None
    stat = "corners" if "corner" in market else "goals"
    period = "half_time" if market.endswith("_ht") else "full_time"
    state = fit_poisson_total(train, stat=stat, period=period)
    if state is None:
        return None
    team_role = team_role_from_market(market)
    if team_role:
        return predict_team_mu(state, match, team_role=team_role)
    return predict_match_mu(state, match)


def _revised_side_ev(
    match: dict[str, Any],
    row: dict[str, Any],
    train: list[dict[str, Any]],
) -> tuple[float | None, float | None, str | None]:
    market = row["market"]
    if "corner" not in market and "goal" not in market:
        return None, None, None
    stat = "corners" if "corner" in market else "goals"
    period = "half_time" if market.endswith("_ht") else "full_time"
    state = fit_poisson_total(train, stat=stat, period=period)
    if state is None:
        return None, None, None
    team_role = team_role_from_market(market)
    p_over = predict_side_probability(state, match, line_raw=row["line"], side="over", team_role=team_role)
    p_under = predict_side_probability(state, match, line_raw=row["line"], side="under", team_role=team_role)
    if p_over >= p_under:
        side = "over"
        p_model = p_over
    else:
        side = "under"
        p_model = p_under
    ev = p_model * row["odds"] - 1.0 if side == row["side"] else p_model * row["odds"] - 1.0
    # EV for the recommended side specifically
    p_side = p_over if row["side"] == "over" else p_under
    return round(p_side, 4), round(p_side * row["odds"] - 1.0, 4), side


def style_note_from_form(form: dict[str, Any], row: dict[str, Any]) -> str:
    market = row["market"]
    side = row["side"]
    notes: list[str] = []

    if "corner" in market:
        key = "corner_ht" if market.endswith("_ht") else "corner_ft"
        home_avg = form.get(f"{key}_home_avg")
        away_avg = form.get(f"{key}_away_avg")
        if home_avg is not None and away_avg is not None:
            total_avg = home_avg + away_avg
            if side == "over" and total_avg < 4:
                notes.append(f"recent corner avg {total_avg:.1f} — low vs over")
            if side == "under" and total_avg >= 5:
                notes.append(f"recent corner avg {total_avg:.1f} — supports under")
            if side == "over" and total_avg >= 5:
                notes.append(f"recent corner avg {total_avg:.1f} — supports over")
    if "goal" in market:
        key = "goal_ht" if market.endswith("_ht") else "goal_ft"
        home_avg = form.get(f"{key}_home_avg")
        away_avg = form.get(f"{key}_away_avg")
        if home_avg is not None and away_avg is not None:
            total_avg = home_avg + away_avg
            if side == "under" and total_avg <= 1.2:
                notes.append(f"recent goals avg {total_avg:.1f} — low-scoring trend")
            if side == "over" and total_avg >= 2.5:
                notes.append(f"recent goals avg {total_avg:.1f} — high-scoring trend")

    return "; ".join(notes) if notes else ""


def analyze(
    bets: list[dict[str, Any]],
    *,
    history: list[dict[str, Any]],
    targets_by_id: dict[str, dict[str, Any]],
    play_styles: dict[str, str],
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    form_cache: dict[str, dict[str, Any]] = {}

    for bet in bets:
        mid = bet["match_id"]
        target = targets_by_id.get(mid)
        if target is None:
            target = {
                "date": bet["date"].replace("-", "/") if "-" in bet["date"] else bet["date"],
                "match_id": mid,
                "teams": bet["teams"],
                "competition": bet.get("competition") or "",
            }
            if len(target["date"]) == 10 and target["date"][4] == "-":
                y, m, d = target["date"].split("-")
                target["date"] = f"{d}/{m}/{y}"

        if mid not in form_cache:
            cutoff = parse_match_date(target["date"])
            train = [m for m in history if parse_match_date(m["date"]) < cutoff]
            form_cache[mid] = match_form_summary(history, target, before=cutoff, limit=limit)

        form = form_cache[mid]
        cutoff = parse_match_date(target["date"])
        train = [m for m in history if parse_match_date(m["date"]) < cutoff]
        mu = _model_mu_for_bet(target, bet["market"], train)
        p_new, ev_new, model_side = _revised_side_ev(target, bet, train)

        pair = split_teams(bet["teams"]) or ("", "")
        style_home = play_styles.get(pair[0], "")
        style_away = play_styles.get(pair[1], "")
        style_combined = " | ".join(x for x in (style_home, style_away) if x)

        form_note = style_note_from_form(form, bet)
        verdict_parts = []
        if form_note:
            verdict_parts.append(form_note)
        if p_new is not None and p_new < 0.45 and bet["side"] in ("over", "under"):
            verdict_parts.append(f"revised model P({bet['side']})={p_new:.2f} weak")
        if model_side and model_side != bet["side"]:
            verdict_parts.append(f"revised model prefers {model_side}")

        rows.append(
            {
                **bet,
                "pick": _fmt_pick(bet),
                "home_team": pair[0],
                "away_team": pair[1],
                "corner_ft_home_last5": form.get("corner_ft_home_summary", ""),
                "corner_ft_away_last5": form.get("corner_ft_away_summary", ""),
                "corner_ht_home_last5": form.get("corner_ht_home_summary", ""),
                "corner_ht_away_last5": form.get("corner_ht_away_summary", ""),
                "goal_ft_home_last5": form.get("goal_ft_home_summary", ""),
                "goal_ft_away_last5": form.get("goal_ft_away_summary", ""),
                "goal_ht_home_last5": form.get("goal_ht_home_summary", ""),
                "goal_ht_away_last5": form.get("goal_ht_away_summary", ""),
                "corner_ft_home_avg": form.get("corner_ft_home_avg", ""),
                "corner_ft_away_avg": form.get("corner_ft_away_avg", ""),
                "corner_ft_home_match_total_avg": form.get("corner_ft_home_match_total_avg", ""),
                "corner_ft_away_match_total_avg": form.get("corner_ft_away_match_total_avg", ""),
                "corner_ht_home_avg": form.get("corner_ht_home_avg", ""),
                "corner_ht_away_avg": form.get("corner_ht_away_avg", ""),
                "corner_ht_home_match_total_avg": form.get("corner_ht_home_match_total_avg", ""),
                "corner_ht_away_match_total_avg": form.get("corner_ht_away_match_total_avg", ""),
                "goal_ft_home_avg": form.get("goal_ft_home_avg", ""),
                "goal_ft_away_avg": form.get("goal_ft_away_avg", ""),
                "goal_ft_home_match_total_avg": form.get("goal_ft_home_match_total_avg", ""),
                "goal_ft_away_match_total_avg": form.get("goal_ft_away_match_total_avg", ""),
                "goal_ht_home_match_total_avg": form.get("goal_ht_home_match_total_avg", ""),
                "goal_ht_away_match_total_avg": form.get("goal_ht_away_match_total_avg", ""),
                "revised_mu": round(mu, 3) if mu is not None else "",
                "revised_p_side": p_new if p_new is not None else "",
                "revised_ev_side": ev_new if ev_new is not None else "",
                "revised_model_side": model_side or "",
                "play_style_home": style_home,
                "play_style_away": style_away,
                "form_verdict": "; ".join(verdict_parts),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze recommended bets with recent form and play-style notes")
    parser.add_argument("--bets", type=Path, default=ROOT / "output" / "tomorrow_20260704_all_bets.json")
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--styles", type=Path, default=None, help="JSON map team -> play style note")
    parser.add_argument("--out-json", type=Path, default=ROOT / "output" / "bet_analysis_with_form.json")
    parser.add_argument("--out-csv", type=Path, default=ROOT / "output" / "bet_analysis_with_form.csv")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    bets_payload = json.loads(args.bets.read_text(encoding="utf-8"))
    bets = [b for b in bets_payload if b.get("bet") == "BET"]

    snapshot = args.snapshot or latest_snapshot(date="2026-07-03")
    targets = load_target_matches(snapshot) if snapshot else []
    targets_by_id = {m["match_id"]: m for m in targets}

    play_styles: dict[str, str] = {}
    if args.styles and args.styles.exists():
        play_styles = json.loads(args.styles.read_text(encoding="utf-8"))

    history = load_matches()
    rows = analyze(bets, history=history, targets_by_id=targets_by_id, play_styles=play_styles, limit=args.limit)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if rows:
        with args.out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print(f"Analyzed {len(rows)} BET rows -> {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
