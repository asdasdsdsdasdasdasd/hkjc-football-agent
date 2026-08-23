"""Closing-line value helpers for saved odds snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.betting.markets import get_adapters
from pipeline.betting.types import BetOpportunity


@dataclass(frozen=True)
class ClvRecord:
    match_id: str
    teams: str
    market: str
    line: str
    side: str
    entry_odds: float
    closing_odds: float
    clv: float

    def to_row(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "teams": self.teams,
            "market": self.market,
            "line": self.line,
            "side": self.side,
            "entry_odds": round(self.entry_odds, 4),
            "closing_odds": round(self.closing_odds, 4),
            "clv": round(self.clv, 4),
        }


def _key(opportunity: BetOpportunity) -> tuple[str, str, str, str]:
    return (opportunity.market, opportunity.line, opportunity.side, opportunity.match_id)


def compare_clv(
    *,
    entry_matches: list[dict[str, Any]],
    closing_matches: list[dict[str, Any]],
    market_keys: list[str],
) -> list[ClvRecord]:
    adapters = get_adapters(market_keys)
    closing_by_id = {m.get("match_id"): m for m in closing_matches}
    rows: list[ClvRecord] = []

    for entry_match in entry_matches:
        match_id = entry_match.get("match_id")
        closing_match = closing_by_id.get(match_id)
        if closing_match is None:
            continue

        closing_opps: dict[tuple[str, str, str, str], BetOpportunity] = {}
        for adapter in adapters:
            for opp in adapter.extract_opportunities(closing_match):
                closing_opps[_key(opp)] = opp

        for adapter in adapters:
            for entry_opp in adapter.extract_opportunities(entry_match):
                closing_opp = closing_opps.get(_key(entry_opp))
                if closing_opp is None or closing_opp.decimal_odds <= 0:
                    continue
                rows.append(
                    ClvRecord(
                        match_id=entry_opp.match_id,
                        teams=entry_opp.teams,
                        market=entry_opp.market,
                        line=entry_opp.line,
                        side=entry_opp.side,
                        entry_odds=entry_opp.decimal_odds,
                        closing_odds=closing_opp.decimal_odds,
                        clv=entry_opp.decimal_odds / closing_opp.decimal_odds - 1.0,
                    )
                )

    rows.sort(key=lambda r: (-r.clv, r.match_id, r.market, r.line, r.side))
    return rows
