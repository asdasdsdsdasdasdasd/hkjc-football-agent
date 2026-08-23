"""HKJC team name translation for external provider matching."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

DEFAULT_ALIAS_PATH = Path("pipeline/data/team_aliases_hkjc.json")

# Inline fallbacks when JSON has not been built yet.
_FALLBACK_ALIASES: dict[str, str] = {
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
    "Cape Verde Islands": "Cabo Verde",
    "Cape Verde": "Cabo Verde",
    "USA": "United States",
    "Utd": "United",
    "Incheon Utd": "Incheon United",
    "AIK Solna": "AIK",
}


def _fold_ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


@lru_cache(maxsize=4)
def load_team_aliases(path: str | None = None) -> dict[str, str]:
    alias_path = Path(path) if path else DEFAULT_ALIAS_PATH
    merged = dict(_FALLBACK_ALIASES)
    if alias_path.is_file():
        payload = json.loads(alias_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            if isinstance(payload.get("aliases"), dict):
                merged.update(payload["aliases"])
            else:
                merged.update({k: v for k, v in payload.items() if k not in ("version", "source_ch", "source_en", "count")})
    return merged


def translate_team_name(name: str, aliases: dict[str, str] | None = None) -> str:
    table = aliases if aliases is not None else load_team_aliases()
    current = name.strip()
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        nxt = table.get(current)
        if not nxt or nxt == current:
            return current
        current = nxt.strip()
    return current


def norm_tokens(name: str, aliases: dict[str, str] | None = None) -> set[str]:
    table = aliases if aliases is not None else load_team_aliases()
    text = translate_team_name(name.strip(), table).casefold()
    text = _fold_ascii(text)
    text = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", text)
    stopwords = {"fc", "sc", "afc", "cf", "club", "team", "women", "w", "u21", "u23", "b"}
    tokens = {t for t in text.split() if t and t not in stopwords}
    if not tokens and text.strip():
        tokens = {text.strip()}
    return tokens
