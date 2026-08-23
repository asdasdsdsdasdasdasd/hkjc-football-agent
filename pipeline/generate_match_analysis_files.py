#!/usr/bin/env python3
"""Generate one markdown analysis file per match."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_play_styles(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _match_record_line(bets: list[dict[str, Any]]) -> str:
    settled = [b for b in bets if b.get("result") not in (None, "", "pending")]
    if not settled:
        return "pending"
    wins = sum(1 for b in settled if b.get("result") == "WIN")
    losses = sum(1 for b in settled if b.get("result") == "LOSE")
    pnl = sum(float(b["pnl"]) for b in settled if b.get("pnl") not in ("", None))
    return f"{wins}W-{losses}L · P&L {pnl:+.2f}u"


def _render_bets(bets: list[dict[str, Any]]) -> str:
    lines = ["| # | Pick | Mkt EV | Old EV | Model P | μ | Result | P&L |", "|---|------|--------|--------|---------|---|--------|-----|"]
    for i, b in enumerate(sorted(bets, key=lambda x: -x["market_ev"]), 1):
        mu = b.get("revised_mu") or b.get("model_mu") or ""
        p = b.get("model_p_side") or b.get("revised_p_side") or ""
        lines.append(
            f"| {i} | {b['pick']} | {b['market_ev']:.4f} | {b['old_ev']:.3f} | {p} | {mu} | {b.get('result', 'pending')} | {b.get('pnl', '')} |"
        )
    return "\n".join(lines)


def _render_form(first: dict[str, Any]) -> str:
    rows = [
        ("Corner HT — Home (at home)", first.get("corner_ht_home_last5", "")),
        ("Corner HT — Away (away)", first.get("corner_ht_away_last5", "")),
        ("Corner FT — Home (at home)", first.get("corner_ft_home_last5", "")),
        ("Corner FT — Away (away)", first.get("corner_ft_away_last5", "")),
        ("Goal FT — Home (at home)", first.get("goal_ft_home_last5", "")),
        ("Goal FT — Away (away)", first.get("goal_ft_away_last5", "")),
        ("Goal HT — Home (at home)", first.get("goal_ht_home_last5", "")),
        ("Goal HT — Away (away)", first.get("goal_ht_away_last5", "")),
    ]
    lines = ["Format: `team/total(date)` — team stat / match total (home+away).", ""]
    for label, val in rows:
        lines.append(f"- **{label}**: {val or 'n/a'}")
    return "\n".join(lines)


def render_match_md(
    match_id: str,
    bets: list[dict[str, Any]],
    *,
    play_styles: dict[str, str],
) -> str:
    first = bets[0]
    home = first["home_team"]
    away = first["away_team"]
    comp = first.get("competition") or "—"
    style_home = play_styles.get(home, "_No play-style note yet._")
    style_away = play_styles.get(away, "_No play-style note yet._")

    score = ""
    if first.get("ft_score"):
        score = f"FT {first['ft_score']} · HT {first.get('ht_score', '')}"
        if first.get("corners_ft") not in (None, ""):
            score += f" · Corners FT {first['corners_ft']} HT {first.get('corners_ht', '')}"
    else:
        score = "pending"

    verdicts = sorted({b.get("form_verdict") for b in bets if b.get("form_verdict")})

    parts = [
        f"# {match_id} — {first['teams']}",
        "",
        f"**Competition:** {comp}  ",
        f"**Date:** {first['date']}  ",
        f"**BET lines:** {len(bets)}  ",
        f"**Match record:** {_match_record_line(bets)}  ",
        f"**7/4 result:** {score}",
        "",
        "## Play Style",
        "",
        f"### {home} (Home)",
        "",
        style_home,
        "",
        f"### {away} (Away)",
        "",
        style_away,
        "",
        "## Recent Form (last 5 in role)",
        "",
        _render_form(first),
        "",
    ]
    if verdicts:
        parts.extend(["## Form vs Bet Notes", "", *[f"- {v}" for v in verdicts], ""])

    parts.extend(
        [
            "## Recommended Bets",
            "",
            _render_bets(bets),
            "",
            "---",
            f"_Generated from bet_analysis_newmodel_form.json · match_id {match_id}_",
        ]
    )
    return "\n".join(parts)


def generate(
    bets_path: Path,
    styles_path: Path,
    out_dir: Path,
) -> list[dict[str, str]]:
    rows = json.loads(bets_path.read_text(encoding="utf-8"))
    play_styles = _load_play_styles(styles_path)
    by_mid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_mid[row["match_id"]].append(row)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()
    index: list[dict[str, str]] = []
    for match_id in sorted(by_mid):
        bets = by_mid[match_id]
        content = render_match_md(match_id, bets, play_styles=play_styles)
        out_file = out_dir / f"{match_id}.md"
        out_file.write_text(content, encoding="utf-8")
        rel = out_file.relative_to(ROOT).as_posix()
        index.append(
            {
                "match_id": match_id,
                "teams": bets[0]["teams"],
                "competition": bets[0].get("competition") or "",
                "path": rel,
                "abs_path": str(out_file),
            }
        )
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate per-match analysis markdown files")
    parser.add_argument(
        "--bets",
        type=Path,
        default=ROOT / "output" / "bet_analysis_newmodel_form.json",
    )
    parser.add_argument(
        "--styles",
        type=Path,
        default=ROOT / "output" / "play_styles_all_20260704.json",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "output" / "match_analysis_20260704",
    )
    args = parser.parse_args(argv)
    if not args.styles.exists():
        fallback = ROOT / "output" / "play_styles_web_20260704.json"
        if fallback.exists():
            args.styles = fallback
    index = generate(args.bets, args.styles, args.out_dir)
    print(f"Wrote {len(index)} match files -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
