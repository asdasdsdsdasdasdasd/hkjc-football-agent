#!/usr/bin/env python3
"""Build HKJC Chinese→English team alias map from CH+EN snapshot pairs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.betting.recommend import load_target_matches
from pipeline.betting.load import split_teams

DEFAULT_OUT = Path("pipeline/data/team_aliases_hkjc.json")

# Gaps not covered by paired EN snapshots (women's, Chile/Brazil, BSD naming).
MANUAL_ZH_TO_EN: dict[str, str] = {
    "競賽會女足": "Racing Club",
    "颶風隊女足": "Huracán",
    "紐維爾舊生女足": "Newell's Old Boys",
    "貝格拉諾女足": "Belgrano",
    "聖羅倫素女足": "San Lorenzo",
    "獨立隊女足": "Independiente",
    "聖菲利普聯": "Unión San Felipe",
    "卡拉雷聯": "Deportes Copiapó",
    "基斯奧馬": "Cuiabá",
    "利斯菲體育會": "Sport Recife",
    "隆德里納": "Londrina",
    "路禾利桑天奴": "Luverdense",
    "戈亞尼恩斯": "Goiás",
    "基路達": "Deportes Copiapó",
    "維拿迪馬愛華頓": "Everton de Viña del Mar",
    "伊基克": "Deportes Iquique",
    "哥甘保": "Cobresal",
    "戈亞斯": "Goiás",
    "施亞拉": "Ceará",
    "天主教大學": "Universidad Católica",
    "哥比亞普": "Colo-Colo",
    "譚柏灣暴徒": "Tampa Bay Rowdies",
    "列星頓SC": "Lexington SC",
    "羅德島FC": "Rhode Island FC",
    "匹茲堡獵犬": "Pittsburgh Riverhounds",
    "布魯克林FC": "Brooklyn FC",
    "路易斯維爾城": "Louisville City FC",
    "哈特福體育": "Hartford Athletic",
    "新墨西哥聯": "New Mexico United",
    "奧克蘭根源": "Oakland Roots SC",
    "拉斯維加斯燈火": "Las Vegas Lights",
    "艾爾帕索火車頭": "El Paso Locomotive FC",
    "蒙特雷灣": "Monterey Bay FC",
    "科羅拉多泉坡路": "Colorado Springs Switchbacks FC",
    "鳳凰城復活": "Phoenix Rising FC",
    "印地安納十一": "Indy Eleven",
    "查勒斯頓炮台": "Charleston Battery",
    "哥登堡": "IFK Göteborg",
    "AIK蘇納": "AIK",
    "蔚山HD": "Ulsan HD",
    "仁川聯": "Incheon United",
    "布里斯班獅吼B隊": "Brisbane Roar Youth",
    "薩克拉門托共和": "Sacramento Republic FC",
    "聖地亞哥波浪女足": "San Diego Wave FC",
    "新澤西紐約高譚女足": "NJ/NY Gotham FC",
    "奧基迪": "Östers IF",
}

# World Cup + national teams (BSD uses lowercase English).
NATIONAL_ZH_TO_EN: dict[str, str] = {
    "澳洲": "Australia",
    "埃及": "Egypt",
    "阿根廷": "Argentina",
    "佛得角": "Cape Verde Islands",
    "哥倫比亞": "Colombia",
    "加納": "Ghana",
    "加拿大": "Canada",
    "摩洛哥": "Morocco",
    "巴拉圭": "Paraguay",
    "法國": "France",
    "巴西": "Brazil",
    "挪威": "Norway",
    "墨西哥": "Mexico",
    "英格蘭": "England",
    "葡萄牙": "Portugal",
    "西班牙": "Spain",
    "美國": "United States",
    "比利時": "Belgium",
}

# HKJC EN label → provider canonical name (USL, K League, Allsvenskan, etc.).
EN_CANONICAL: dict[str, str] = {
    "Incheon Utd": "Incheon United",
    "Sacramento Republic": "Sacramento Republic FC",
    "Colorado Springs Switchbacks": "Colorado Springs Switchbacks FC",
    "Brisbane Roar B": "Brisbane Roar Youth",
    "AIK Solna": "AIK",
    "Cape Verde": "Cape Verde Islands",
    "Cape Verde Islands": "Cabo Verde",
    "USA": "United States",
    "Utd": "United",
}


def _pair_aliases(ch_matches: list[dict], en_by_id: dict[str, dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in ch_matches:
        pair = split_teams(match.get("teams") or "")
        en_match = en_by_id.get(match.get("match_id", ""))
        if not pair or not en_match:
            continue
        en_pair = split_teams(en_match.get("teams") or "")
        if not en_pair:
            continue
        out[pair[0]] = en_pair[0]
        out[pair[1]] = en_pair[1]
    return out


def build_alias_map(
    ch_snapshot: Path,
    en_snapshot: Path | None = None,
) -> dict[str, str]:
    ch_matches = load_target_matches(ch_snapshot)
    zh_to_en: dict[str, str] = {}
    zh_to_en.update(NATIONAL_ZH_TO_EN)

    if en_snapshot and en_snapshot.is_file():
        en_matches = load_target_matches(en_snapshot)
        en_by_id = {m["match_id"]: m for m in en_matches}
        zh_to_en.update(_pair_aliases(ch_matches, en_by_id))

    zh_to_en.update(MANUAL_ZH_TO_EN)
    zh_to_en.update(EN_CANONICAL)
    return dict(sorted(zh_to_en.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build HKJC team alias JSON from CH+EN snapshots")
    parser.add_argument("--ch-snapshot", type=Path, required=True, help="Chinese HKJC snapshot JSON")
    parser.add_argument("--en-snapshot", type=Path, default=None, help="English HKJC snapshot JSON")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output alias JSON path")
    args = parser.parse_args()

    aliases = build_alias_map(args.ch_snapshot, args.en_snapshot)
    payload = {
        "version": 1,
        "source_ch": str(args.ch_snapshot),
        "source_en": str(args.en_snapshot) if args.en_snapshot else None,
        "count": len(aliases),
        "aliases": aliases,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(aliases)} aliases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
