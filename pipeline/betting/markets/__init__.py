"""Market adapter registry."""

from __future__ import annotations

from pipeline.betting.markets.corner_ou import CORNER_ADAPTERS
from pipeline.betting.markets.goal_ou import GOAL_ADAPTERS
from pipeline.betting.markets.match_1x2 import MATCH_1X2_ADAPTERS
from pipeline.betting.markets.team_ou import TEAM_OU_ADAPTERS, TEAM_OU_MARKET_KEYS
from pipeline.betting.types import MarketAdapter

MATCH_TOTAL_MARKET_KEYS = sorted(
    {**CORNER_ADAPTERS, **GOAL_ADAPTERS, **MATCH_1X2_ADAPTERS}.keys()
)
DEFAULT_MARKET_KEYS = MATCH_TOTAL_MARKET_KEYS + TEAM_OU_MARKET_KEYS
DEFAULT_MARKETS = ",".join(DEFAULT_MARKET_KEYS)

ALL_ADAPTERS: dict[str, MarketAdapter] = {
    **CORNER_ADAPTERS,
    **GOAL_ADAPTERS,
    **MATCH_1X2_ADAPTERS,
    **TEAM_OU_ADAPTERS,
}


def get_adapters(keys: list[str]) -> list[MarketAdapter]:
    out: list[MarketAdapter] = []
    for key in keys:
        key = key.strip()
        if not key:
            continue
        adapter = ALL_ADAPTERS.get(key)
        if adapter is None:
            known = ", ".join(sorted(ALL_ADAPTERS))
            raise ValueError(f"Unknown market {key!r}. Known: {known}")
        out.append(adapter)
    if not out:
        raise ValueError("No markets selected")
    return out


def list_markets() -> list[str]:
    return sorted(ALL_ADAPTERS)
