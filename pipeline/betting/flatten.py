"""Flatten match odds into BetOpportunity rows via market adapters."""

from __future__ import annotations

from typing import Any

from pipeline.betting.markets import get_adapters
from pipeline.betting.types import BetOpportunity


def flatten_opportunities(
    matches: list[dict[str, Any]],
    market_keys: list[str],
) -> list[BetOpportunity]:
    adapters = get_adapters(market_keys)
    out: list[BetOpportunity] = []
    for match in matches:
        for adapter in adapters:
            out.extend(adapter.extract_opportunities(match))
    out.sort(key=lambda o: (o.date, o.match_id, o.market, o.line, o.side))
    return out
