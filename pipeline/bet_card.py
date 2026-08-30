"""Comprehensive bet card: DC model + odds movement + LLM synthesis.

For each candidate bet (from a predict_v32 live book), join:
  - model probability / EV (DC model, already in the book row)
  - odds movement from odds-history (opening -> now, steam jumps)
  - LLM verdict from local Qwen (structured JSON)

Usage:
  python3 -m pipeline.bet_card --date 2026-08-30 \
      --book output/tomorrow_20260830_v33_c20_live.json
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from pipeline import llm, match_intel, odds_moves

ROOT = Path(__file__).resolve().parent.parent
INTEL_DIR = ROOT / "intel"

PROMPT_TMPL = """You are a football betting analyst for HKJC pools (Hong Kong odds, 2-way over/under).
Assess this candidate under bet. Be skeptical: HKJC takes ~18% margin on O/U pools.

Match: {teams} ({league}), kickoff {kickoff}
Market: {market_desc}
Current odds: {odds} (implied {implied:.1%})
Model (Dixon-Coles, time-decayed attack/defense): P(under) = {p_model:.1%} -> EV = {ev:+.1%}
Opening odds: {open_odds} | Move: {move_desc}
Line context: main line {line}

Team news / predicted lineup / injuries (scraped; lineup may be predicted not confirmed):
{intel}

Rules:
- Missing attackers or a defensive lineup strengthens an under; missing CBs or a very attacking XI weakens it.
- Fresh injury news that the market has not moved on is more useful than stale rumours.
- If odds are shortening on the under, the market agrees with the model.
- If odds are drifting on the under, money is on the over — be cautious.
- EV below +3% is within model noise; above +8% is suspicious (check data).
- Do not invent players or injuries that are not in the intel block.

Return JSON only: {{"verdict": "bet"|"pass"|"lean", "confidence": 0.0-1.0, "reason": "<=40 words", "risk_flags": ["..."]}}"""


def _market_desc(market: str, line: str) -> str:
    names = {
        "corner_ou": f"FT corners under {line}",
        "goal_ou_ht": f"HT goals under {line}",
        "goal_ou": f"FT goals under {line}",
        "corner_ou_ht": f"HT corners under {line}",
    }
    return names.get(market, f"{market} under {line}")


def _move_for(row: dict[str, Any], moves: list[dict[str, Any]]) -> dict[str, Any] | None:
    mid = row.get("match_id")
    market = row.get("market")
    line = str(row.get("line") or "").strip("[]")
    best = None
    for mv in moves:
        if mv["match_id"] != mid or mv["market"] != market or mv["side"] != "L":
            continue
        mv_line = str(mv["line"]).strip("[]")
        if mv_line == line or (mv.get("main") and best is None):
            if mv_line == line:
                return mv
            best = mv
    return best


def load_intel_map(path: Path | None = None) -> dict[str, Any]:
    if path and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("matches") or {}
    if not INTEL_DIR.exists():
        return {}
    files = sorted(INTEL_DIR.glob("intel-*.json"))
    if not files:
        return {}
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    return payload.get("matches") or {}


def build_card(book: list[dict[str, Any]], *, move_date: str, use_llm: bool = True,
               intel_map: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    moves = odds_moves.summarize(move_date)
    intel_map = intel_map if intel_map is not None else load_intel_map()
    card: list[dict[str, Any]] = []
    for row in book:
        entry = dict(row)
        mv = _move_for(row, moves)
        if mv:
            entry["odds_open"] = mv["open"]
            entry["odds_move_pp"] = mv["move_pp"]
            entry["odds_max_jump_pp"] = mv["max_jump_pp"]
        p_model = row.get("model_p_side") or row.get("revised_prob") or row.get("model_prob")
        odds = row.get("odds")
        try:
            p_model_f = float(p_model)
            odds_f = float(odds)
            entry["ev"] = round(p_model_f * odds_f - 1.0, 4)
        except (TypeError, ValueError):
            entry["ev"] = None

        if use_llm and llm.health() and entry.get("ev") is not None:
            if mv:
                move_desc = (f"under odds {mv['open']} -> {mv['now']} "
                             f"({'shortened' if mv['move_pp'] > 0 else 'drifted'} {abs(mv['move_pp']):.1f}pp, "
                             f"max jump {mv['max_jump_pp']:.1f}pp, {mv['n_changes']} changes)")
                open_odds = mv["open"]
            else:
                move_desc = "no movement tracked yet"
                open_odds = odds
            intel_row = intel_map.get(str(row.get("match_id"))) or {}
            entry["intel"] = {
                "fotmob": (intel_row.get("fotmob") or {}).get("id") if intel_row else None,
                "lineup_type": intel_row.get("lineup_type"),
                "unavailable": {
                    "home": (intel_row.get("home_xi") or {}).get("unavailable") or [],
                    "away": (intel_row.get("away_xi") or {}).get("unavailable") or [],
                },
                "headlines": [h.get("title") for h in (intel_row.get("headlines") or [])[:6]],
            }
            prompt = PROMPT_TMPL.format(
                teams=row.get("teams"), league=row.get("competition") or row.get("league") or "?",
                kickoff=row.get("kick_off") or row.get("date"),
                market_desc=_market_desc(str(row.get("market")), str(row.get("line"))),
                odds=odds, implied=1.0 / odds_f, p_model=p_model_f, ev=entry["ev"],
                open_odds=open_odds, move_desc=move_desc, line=row.get("line"),
                intel=match_intel.format_brief(intel_row),
            )
            try:
                verdict = llm.chat_json([{"role": "user", "content": prompt}], max_tokens=1500)
                entry["llm"] = verdict
            except Exception as e:  # noqa: BLE001
                entry["llm"] = {"verdict": "error", "reason": str(e)[:120]}
        card.append(entry)
    return card


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--book", type=Path, required=True)
    ap.add_argument("--move-date", default=date.today().isoformat())
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--intel", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    book = json.loads(args.book.read_text(encoding="utf-8"))
    card = build_card(book, move_date=args.move_date, use_llm=not args.no_llm,
                      intel_map=load_intel_map(args.intel))
    out = args.out or (ROOT / "output" / f"card_{args.date.replace('-', '')}.json")
    out.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"card ({len(card)}) -> {out}")
    for e in card:
        v = (e.get("llm") or {}).get("verdict", "-")
        mv = e.get("odds_move_pp")
        print(f"{e.get('match_id')} {str(e.get('teams'))[:26]:26} {e.get('pick')} "
              f"ev={e.get('ev')} move={mv} llm={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
