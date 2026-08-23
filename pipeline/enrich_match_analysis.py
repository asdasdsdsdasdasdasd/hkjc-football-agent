#!/usr/bin/env python3
"""Enrich thin match analysis markdown with bilingual structured sections."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.generate_match_analysis_files import (  # noqa: E402
    _match_record_line,
    _render_bets,
    _render_form,
)
from pipeline.team_aliases import load_team_aliases, translate_team_name  # noqa: E402

WC_CONTEXT_PATH = Path(__file__).parent / "data" / "wc_enrichment_context.json"

LEAGUE_HINTS: list[tuple[str, str, str]] = [
    (r"女足|Women", "Women's football", "女子足球"),
    (r"FC首爾|FC光州|仁川|蔚山|FC大邱|FC江原|FC水原|FC安養|FC富川|FC城南", "K League 1", "韩国K1联赛"),
    (r"十一|炮台|暴徒|羅德島|獵犬|燈火|火車頭|聖安東尼奧|蒙特雷|路易斯維爾|哈特福|坡路|復活|布魯克林|新墨西哥|奧克蘭根源", "USL Championship", "美国USL冠军联赛"),
    (r"布里斯班|黑鎮|悉尼|馬可尼|聖佐治|NWS精神|洛克達爾", "A-League / NPL", "澳大利亚联赛体系"),
    (r"哥登堡|AIK|TPS|VPS|伊斯韋斯", "Allsvenskan / Nordic", "北欧联赛"),
    (r"天主教|聖菲利普|伊基克|哥甘保|卡拉雷|哥比亞普", "Chilean league", "智利联赛"),
    (r"隆德里納|CRB|戈亞斯|施亞拉|基路達|維拿迪馬", "Brazilian league", "巴西联赛"),
    (r"競賽會|颶風|紐維爾|貝格拉諾", "Argentina women's / Liga", "阿根廷联赛"),
]


def _is_enriched(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if "## 1. 比赛概览" in text or "## 一、" in text:
        return True
    return len(text.splitlines()) > 120


def _team_en(name: str, aliases: dict[str, str]) -> str:
    return translate_team_name(name.strip(), aliases)


def _infer_league(home: str, away: str, comp: str) -> tuple[str, str]:
    if comp and comp not in ("—", ""):
        return comp, comp
    blob = f"{home} {away}"
    for pattern, en, zh in LEAGUE_HINTS:
        if re.search(pattern, blob):
            return f"{en} / {zh}", en
    return "Club football / 俱乐部赛事", "Club"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _features_by_mid(path: Path) -> dict[str, dict[str, Any]]:
    rows = _load_json(path, [])
    return {r["match_id"]: r for r in rows if r.get("match_id")}


def _expand_style(text: str) -> list[str]:
    if not text or text.startswith("_No play"):
        return ["_No detailed play-style note._"]
    parts = re.split(r"\s*[—;]\s*", text)
    return [p.strip() for p in parts if p.strip()]


def _fmt_avg(label: str, val: Any) -> str:
    if val in (None, "", 0):
        return f"| {label} | — | — |"
    try:
        v = float(val)
        return f"| {label} | {v:.2f} | — |"
    except (TypeError, ValueError):
        return f"| {label} | {val} | — |"


def _fmt_num(val: Any, places: int = 2) -> str:
    if val in (None, ""):
        return "—"
    try:
        return f"{float(val):.{places}f}"
    except (TypeError, ValueError):
        return str(val)


def _build_context_table(first: dict[str, Any], feat: dict[str, Any] | None) -> str:
    hx = feat.get("home_xg") if feat else None
    ax = feat.get("away_xg") if feat else None
    wx = feat.get("weather_goal_factor") if feat else None
    lines = [
        "| 指标 / Metric | Home 主队 | Away 客队 |",
        "|---------------|-----------|-----------|",
        f"| Corner FT avg (team) | {first.get('corner_ft_home_avg', '—')} | {first.get('corner_ft_away_avg', '—')} |",
        f"| Corner FT avg (match total) | {first.get('corner_ft_home_match_total_avg', '—')} | {first.get('corner_ft_away_match_total_avg', '—')} |",
        f"| Corner HT avg (match total) | {first.get('corner_ht_home_match_total_avg', '—')} | {first.get('corner_ht_away_match_total_avg', '—')} |",
        f"| Goal FT avg (team) | {first.get('goal_ft_home_avg', '—')} | {first.get('goal_ft_away_avg', '—')} |",
        f"| Goal FT avg (match total) | {first.get('goal_ft_home_match_total_avg', '—')} | {first.get('goal_ft_away_match_total_avg', '—')} |",
        f"| Goal HT avg (match total) | {first.get('goal_ht_home_match_total_avg', '—')} | {first.get('goal_ht_away_match_total_avg', '—')} |",
    ]
    if hx is not None or ax is not None:
        lines.append(f"| **BSD xG (pre-match)** | **{_fmt_num(hx)}** | **{_fmt_num(ax)}** |")
        if hx is not None and ax is not None:
            lines.append(f"| **xG total (H+A)** | **{(float(hx) + float(ax)):.2f}** | — |")
    if wx is not None:
        lines.append(f"| Weather goal factor | — | **{_fmt_num(wx)}** |")
    if feat:
        lines.append(
            f"| Lineup strength | {_fmt_num(feat.get('home_lineup_strength'))} | {_fmt_num(feat.get('away_lineup_strength'))} |"
        )
        if feat.get("bsd_source"):
            lines.append(f"| BSD note | {feat.get('bsd_source')} | — |")
    return "\n".join(lines)


def _trend_paragraph(first: dict[str, Any]) -> str:
    bits: list[str] = []
    cf_h = first.get("corner_ft_home_match_total_avg")
    cf_a = first.get("corner_ft_away_match_total_avg")
    gf_h = first.get("goal_ft_home_match_total_avg")
    gf_a = first.get("goal_ft_away_match_total_avg")
    if cf_h and cf_a:
        avg = (float(cf_h) + float(cf_a)) / 2
        tone = "偏高" if avg >= 10 else ("偏低" if avg <= 7 else "中等")
        bits.append(f"双方近期全场角球比赛均值约 **{avg:.1f}**（{tone}）。")
    if gf_h and gf_a:
        avg_g = (float(gf_h) + float(gf_a)) / 2
        tone_g = "大球倾向" if avg_g >= 2.8 else ("小球倾向" if avg_g <= 2.0 else "中性")
        bits.append(f"进球比赛均值约 **{avg_g:.1f}** — {tone_g}。")
    ht_g = first.get("goal_ht_away_match_total_avg") or first.get("goal_ht_home_match_total_avg")
    if ht_g and float(ht_g) >= 1.8:
        bits.append(f"半场进球样本均值 **{ht_g}** — 支持 **HT goal over** 叙事。")
    return " ".join(bits) if bits else "表单样本有限；以模型 μ 与盘口 EV 为主。"


def _model_angles(bets: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for b in sorted(bets, key=lambda x: -float(x.get("old_ev") or 0)):
        pick = b["pick"]
        mu = b.get("revised_mu") or b.get("model_mu") or "—"
        ev = b.get("old_ev", "—")
        p = b.get("model_p_side") or b.get("revised_p_side") or "—"
        verdict = b.get("form_verdict") or ""
        note = f"**{pick}** — model μ={mu}, P={p}, old EV={ev}"
        if verdict:
            note += f"; {verdict}"
        lines.append(f"- {note}")
    return "\n".join(lines)


def _build_revised_block(
    mid: str,
    revised_rows: list[dict[str, Any]],
    revision_log: list[dict[str, Any]],
) -> str:
    block_lines = [
        "",
        "## Revised Recommendation (form + xG + weather + style)",
        "",
        "| Action | Pick | Composite | Rev EV | Form | xG | Weather | Reason |",
        "|--------|------|-----------|--------|------|-----|---------|--------|",
    ]
    rows = [r for r in revised_rows if r["match_id"] == mid]
    drops = [d for d in revision_log if d.get("match_id") == mid and d.get("action") == "DROP"]
    seen_picks = {r["pick"] for r in rows}
    for r in rows:
        block_lines.append(
            f"| {r.get('action', '')} | {r['pick']} | {r['composite_score']} | {r.get('model_ev_side', '')} | "
            f"{r.get('form_bias', '')} | {r.get('xg_bias', '')} | {r.get('weather_bias', '')} | "
            f"{r.get('revision_reason', '')[:80]} |"
        )
    for d in drops:
        if d.get("pick") not in seen_picks:
            block_lines.append(
                f"| DROP | {d.get('pick', '')} | — | — | — | — | — | {d.get('reason', '')[:80]} |"
            )
    if not rows and not drops:
        block_lines.append("| PASS | — | — | — | — | — | — | no line passed revision filters |")
    return "\n".join(block_lines)


def _build_sources_section(ctx: dict[str, Any] | None, league: str) -> str:
    if ctx and ctx.get("sources_table"):
        return "\n".join(["## 8. 多语言信源对照 / Source Language Split", "", ctx["sources_table"], ""])
    return "\n".join(
        [
            "## 8. 多语言信源对照 / Source Language Split",
            "",
            "| 来源 | 语言 | 用途 |",
            "|------|------|------|",
            f"| HKJC form JSON | ZH | 近5场角球/进球角色表单 |",
            f"| BSD xG merged | EN | pre-match xG / weather / lineup |",
            f"| Play style notes | EN | 战术标签（{league}） |",
            f"| Revision engine | mixed | form + xG + weather + style composite |",
            "",
        ]
    )


def _result_label(first: dict[str, Any]) -> str:
    d = first.get("date", "")
    if d:
        parts = d.split("-")
        if len(parts) == 3:
            return f"{parts[1]}/{parts[2]} result"
    return "result"


def _score_line(first: dict[str, Any]) -> str:
    if first.get("ft_score"):
        s = f"FT {first['ft_score']} · HT {first.get('ht_score', '')}"
        if first.get("corners_ft") not in (None, ""):
            s += f" · Corners FT {first['corners_ft']} HT {first.get('corners_ht', '')}"
        return s
    return "pending"


def _render_wc(
    match_id: str,
    bets: list[dict[str, Any]],
    feat: dict[str, Any] | None,
    wc_ctx: dict[str, Any],
    aliases: dict[str, str],
    revised_block: str,
) -> str:
    first = bets[0]
    home, away = first["home_team"], first["away_team"]
    home_en, away_en = _team_en(home, aliases), _team_en(away, aliases)
    ctx = wc_ctx.get(match_id, {})
    style_home = first.get("play_style_home") or ""
    style_away = first.get("play_style_away") or ""

    overview = ctx.get("overview_zh", "")
    overview_en = ctx.get("overview_en", "")
    path_table = ctx.get("path_table", "")
    h2h = ctx.get("h2h", "")
    venue = ctx.get("venue", "USA neutral venue")
    comp = ctx.get("competition", "2026 FIFA World Cup · Round of 16 / 十六強")
    kickoff = ctx.get("kickoff", "")

    parts = [
        f"# {match_id} — {home} 對 {away}（{home_en} vs {away_en} · 2026 世盃十六強）",
        "",
        f"**Competition:** {comp}  ",
        f"**Venue:** {venue}  ",
        f"**Date (HKJC):** {first['date']}  ",
    ]
    if kickoff:
        parts.append(f"**Kick-off:** {kickoff}  ")
    parts.extend(
        [
            f"**HKJC Match ID:** {match_id}  ",
            f"**Note:** {home} = **{home_en}** · {away} = **{away_en}** · HKJC 主客标记（合办国中立场地）",
            "",
            f"**BET lines:** {len(bets)}  ",
            f"**Match record:** {_match_record_line(bets)}  ",
            f"**{_result_label(first)}:** {_score_line(first)}",
            "",
            "---",
            "",
            "## 1. 比赛概览 / Match Overview",
            "",
            overview or f"**{home_en}** vs **{away_en}** — 2026 世盃十六強淘汰赛。",
            "",
        ]
    )
    if overview_en:
        parts.extend(["", overview_en, ""])
    if path_table:
        parts.extend(["**晋级路径 / Path to R16**", "", path_table, ""])
    if h2h:
        parts.extend(["**H2H / 历史**", "", h2h, ""])

    parts.extend(
        [
            "---",
            "",
            f"## 2. 主队战术 (Home — {home_en} / {home})",
            "",
        ]
    )
    for bullet in _expand_style(style_home):
        parts.append(f"- {bullet}")
    if ctx.get("home_extra"):
        parts.extend(["", ctx["home_extra"], ""])

    parts.extend(
        [
            "",
            f"## 3. 客队战术 (Away — {away_en} / {away})",
            "",
        ]
    )
    for bullet in _expand_style(style_away):
        parts.append(f"- {bullet}")
    if ctx.get("away_extra"):
        parts.extend(["", ctx["away_extra"], ""])

    parts.extend(
        [
            "",
            "## 4. 对阵情境 / Match Context",
            "",
            _build_context_table(first, feat),
            "",
            "## 5. 角球与进球趋势 / Trends",
            "",
            _trend_paragraph(first),
            "",
            "## 6. 模型视角 / Model Angles",
            "",
            _model_angles(bets),
            "",
            "## 7. 近期表单 / Recent Form",
            "",
            _render_form(first),
            "",
        ]
    )
    verdicts = sorted({b.get("form_verdict") for b in bets if b.get("form_verdict")})
    if verdicts:
        parts.extend(["## Form vs Bet Notes", "", *[f"- {v}" for v in verdicts], ""])
    parts.extend([_build_sources_section(ctx, "World Cup"), ""])

    parts.extend(
        [
            "## Recommended Bets",
            "",
            _render_bets(bets),
            revised_block,
            "",
            "---",
            f"_Enriched from bet_analysis + BSD xG + play styles · match_id {match_id}_",
        ]
    )
    return "\n".join(parts)


def _render_league(
    match_id: str,
    bets: list[dict[str, Any]],
    feat: dict[str, Any] | None,
    aliases: dict[str, str],
    revised_block: str,
) -> str:
    first = bets[0]
    home, away = first["home_team"], first["away_team"]
    home_en, away_en = _team_en(home, aliases), _team_en(away, aliases)
    comp_label, comp_short = _infer_league(home, away, first.get("competition") or "")
    style_home = first.get("play_style_home") or ""
    style_away = first.get("play_style_away") or ""

    parts = [
        f"# {match_id} — {home} 對 {away}（{home_en} vs {away_en}）",
        "",
        f"**Competition:** {comp_label}  ",
        f"**Date:** {first['date']}  ",
        f"**Teams (EN):** {home_en} (H) · {away_en} (A)  ",
        f"**BET lines:** {len(bets)}  ",
        f"**Match record:** {_match_record_line(bets)}  ",
        f"**Result:** {_score_line(first)}",
        "",
        "---",
        "",
        "## 1. 比赛概览 / Match Overview",
        "",
        f"**{comp_short}** 场次：{home_en} 主场对阵 {away_en}。",
        f"HKJC 模型基于近期主客场角色表单、**BSD xG**（如有）及 play-style 标签生成 {len(bets)} 条 BET 线。",
        "",
        f"**战术主轴 / Tactical thread：** {_trend_paragraph(first)}",
        "",
        f"## 2. 主队战术 (Home — {home_en})",
        "",
    ]
    for bullet in _expand_style(style_home):
        parts.append(f"- {bullet}")

    parts.extend([f"", f"## 3. 客队战术 (Away — {away_en})", ""])
    for bullet in _expand_style(style_away):
        parts.append(f"- {bullet}")

    parts.extend(
        [
            "",
            "## 4. 数据对照 / Match Context",
            "",
            _build_context_table(first, feat),
            "",
            "## 5. 角球与进球趋势 / Corner & Goal Trends",
            "",
            _trend_paragraph(first),
            "",
            "## 6. 模型视角 / Model Angles",
            "",
            _model_angles(bets),
            "",
            "## 7. 近期表单 / Recent Form",
            "",
            _render_form(first),
            "",
        ]
    )
    verdicts = sorted({b.get("form_verdict") for b in bets if b.get("form_verdict")})
    if verdicts:
        parts.extend(["## Form vs Bet Notes", "", *[f"- {v}" for v in verdicts], ""])
    parts.extend([_build_sources_section(None, comp_short), ""])

    parts.extend(
        [
            "## Recommended Bets",
            "",
            _render_bets(bets),
            revised_block,
            "",
            "---",
            f"_Enriched from bet_analysis + BSD xG + play styles · match_id {match_id}_",
        ]
    )
    return "\n".join(parts)


def enrich_dir(
    out_dir: Path,
    bets_path: Path,
    features_path: Path,
    revised_path: Path,
    revision_log_path: Path,
    *,
    force: bool = False,
    skip_ids: set[str] | None = None,
) -> list[str]:
    skip_ids = skip_ids or set()
    aliases = load_team_aliases(str(ROOT / "pipeline" / "data" / "team_aliases_hkjc.json"))
    rows = json.loads(bets_path.read_text(encoding="utf-8"))
    by_mid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_mid[row["match_id"]].append(row)

    features = _features_by_mid(features_path)
    revised_rows = _load_json(revised_path, [])
    revision_log = _load_json(revision_log_path, [])
    wc_ctx = _load_json(WC_CONTEXT_PATH, {})

    written: list[str] = []
    for match_id in sorted(by_mid):
        if match_id in skip_ids:
            continue
        out_file = out_dir / f"{match_id}.md"
        if not force and _is_enriched(out_file):
            continue
        bets = by_mid[match_id]
        feat = features.get(match_id)
        revised_block = _build_revised_block(match_id, revised_rows, revision_log)
        is_wc = match_id.startswith("FB301") or "世盃" in (bets[0].get("competition") or "")
        if is_wc and match_id in wc_ctx:
            content = _render_wc(match_id, bets, feat, wc_ctx, aliases, revised_block)
        else:
            content = _render_league(match_id, bets, feat, aliases, revised_block)
        out_file.write_text(content, encoding="utf-8")
        written.append(match_id)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich match analysis markdown files")
    parser.add_argument("--date", default="20260705", help="Date tag e.g. 20260705")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite already-enriched files")
    parser.add_argument("--skip", nargs="*", default=["FB3017"], help="Match IDs to skip")
    args = parser.parse_args(argv)

    tag = args.date
    out_dir = args.out_dir or (ROOT / "output" / f"match_analysis_{tag}")
    bets_path = ROOT / "output" / f"bet_analysis_newmodel_form_{tag}.json"
    if not bets_path.exists():
        bets_path = ROOT / "output" / "bet_analysis_newmodel_form.json"

    features_path = ROOT / "output" / f"external_features_bsd_en_{tag}.json"
    if not features_path.exists():
        features_path = ROOT / "output" / "external_features_bsd_en_merged.json"
    revised_path = ROOT / "output" / f"tomorrow_{tag}_revised_bets.json"
    if not revised_path.exists():
        revised_path = ROOT / "output" / f"tomorrow_{tag[:4]}-{tag[4:6]}-{tag[6:]}_revised_bets.json"
    revision_log_path = ROOT / "output" / f"tomorrow_{tag}_revision_log.json"

    written = enrich_dir(
        out_dir,
        bets_path,
        features_path,
        revised_path,
        revision_log_path,
        force=args.force,
        skip_ids=set(args.skip),
    )
    print(f"Enriched {len(written)} files in {out_dir}: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
