"""Corner over/under market adapters."""

from __future__ import annotations

from typing import Any

from pipeline.betting.devig import implied_over_under
from pipeline.betting.load import parse_match_date
from pipeline.betting.models.poisson_total import (
    PoissonTotalState,
    expected_over_under_roi,
    fair_over_under_odds,
    fit_poisson_total,
    predict_side_probability,
)
from pipeline.betting.settlement import settle_over_under
from pipeline.betting.types import BetOpportunity, BetOutcome, ModelState


def _corner_total(match: dict[str, Any], period: str) -> int | None:
    block = (match.get("corners") or {}).get(period) or {}
    val = block.get("total")
    return int(val) if val is not None else None


class _CornerOuAdapter:
    key: str
    odds_section: str
    period: str

    def extract_opportunities(self, match: dict[str, Any]) -> list[BetOpportunity]:
        rows = (match.get("odds_closing") or {}).get(self.odds_section) or []
        if not rows:
            return []
        d = parse_match_date(match["date"])
        base = {
            "date": d,
            "match_id": match.get("match_id", ""),
            "market": self.key,
            "teams": match.get("teams") or "",
            "competition": match.get("competition"),
        }
        out: list[BetOpportunity] = []
        for entry in rows:
            line = entry.get("line")
            over_odds = entry.get("over_odds")
            under_odds = entry.get("under_odds")
            if not line or over_odds is None or under_odds is None:
                continue
            out.append(
                BetOpportunity(
                    **base,
                    line=str(line),
                    side="over",
                    decimal_odds=float(over_odds),
                    over_odds=float(over_odds),
                    under_odds=float(under_odds),
                )
            )
            out.append(
                BetOpportunity(
                    **base,
                    line=str(line),
                    side="under",
                    decimal_odds=float(under_odds),
                    over_odds=float(over_odds),
                    under_odds=float(under_odds),
                )
            )
        return out

    def settle(self, opportunity: BetOpportunity, match: dict[str, Any]) -> BetOutcome:
        total = _corner_total(match, self.period)
        return settle_over_under(total, opportunity.line, opportunity.side)

    def training_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [m for m in matches if _corner_total(m, self.period) is not None]

    def fit(self, train_matches: list[dict[str, Any]]) -> ModelState | None:
        state = fit_poisson_total(train_matches, stat="corners", period=self.period)
        if state is None:
            return None
        return ModelState(data={"poisson": state})

    def predict(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        p_market = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
        if p_market is not None:
            return p_market
        poisson: PoissonTotalState = state.data["poisson"]
        return predict_side_probability(
            poisson,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
        )

    def expected_roi(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        p_market = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
        if p_market is not None:
            return p_market * opportunity.decimal_odds - 1.0
        poisson: PoissonTotalState = state.data["poisson"]
        return expected_over_under_roi(
            poisson,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            decimal_odds=opportunity.decimal_odds,
        )

    def fair_odds(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float | None:
        p_market = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
        if p_market is not None and p_market > 0:
            return 1.0 / p_market
        poisson: PoissonTotalState = state.data["poisson"]
        return fair_over_under_odds(
            poisson,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
        )


class CornerOuFtAdapter(_CornerOuAdapter):
    key = "corner_ou_ft"
    odds_section = "開出角球大細"
    period = "full_time"


class CornerOuHtAdapter(_CornerOuAdapter):
    key = "corner_ou_ht"
    odds_section = "半場開出角球大細"
    period = "half_time"


CORNER_ADAPTERS = {
    CornerOuFtAdapter.key: CornerOuFtAdapter(),
    CornerOuHtAdapter.key: CornerOuHtAdapter(),
}
