"""Goal over/under market adapter (v5 calibrated opponent-aware totals)."""

from __future__ import annotations

from typing import Any

from pipeline.betting.load import parse_match_date, parse_score_total
from pipeline.betting.models.calibrated_total import (
    expected_calibrated_roi,
    fit_goal_model_state,
    predict_calibrated_probability,
)
from pipeline.betting.settlement import settle_over_under
from pipeline.betting.types import BetOpportunity, BetOutcome, ModelState


def _goal_total(match: dict[str, Any], period: str) -> int | None:
    scores = match.get("scores") or {}
    key = "full_time" if period == "full_time" else "half_time"
    return parse_score_total(scores.get(key))


class _GoalOuAdapter:
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
        total = _goal_total(match, self.period)
        return settle_over_under(total, opportunity.line, opportunity.side)

    def training_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [m for m in matches if _goal_total(m, self.period) is not None]

    def fit(self, train_matches: list[dict[str, Any]]) -> ModelState | None:
        payload = fit_goal_model_state(train_matches, period=self.period)
        if payload is None:
            return None
        return ModelState(data=payload)

    def predict(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        details = predict_calibrated_probability(
            state.data,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            over_odds=opportunity.over_odds,
            under_odds=opportunity.under_odds,
        )
        # Cache diagnostics on the opportunity-less path via match transient field.
        cache = match.setdefault("_v5_goal_diag", {})
        cache[(opportunity.market, opportunity.line, opportunity.side)] = details
        return float(details["p_final"] or 0.0)

    def expected_roi(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        return expected_calibrated_roi(
            state.data,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            decimal_odds=opportunity.decimal_odds,
            over_odds=opportunity.over_odds,
            under_odds=opportunity.under_odds,
        )

    def fair_odds(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float | None:
        p = self.predict(state, opportunity, match)
        if p <= 0:
            return None
        return 1.0 / p

    def predict_details(
        self,
        state: ModelState,
        opportunity: BetOpportunity,
        match: dict[str, Any],
    ) -> dict[str, Any]:
        return predict_calibrated_probability(
            state.data,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            over_odds=opportunity.over_odds,
            under_odds=opportunity.under_odds,
        )


class GoalOuFtAdapter(_GoalOuAdapter):
    key = "goal_ou_ft"
    odds_section = "入球大細"
    period = "full_time"


class GoalOuHtAdapter(_GoalOuAdapter):
    key = "goal_ou_ht"
    odds_section = "半場入球大細"
    period = "half_time"


GOAL_ADAPTERS = {
    GoalOuFtAdapter.key: GoalOuFtAdapter(),
    GoalOuHtAdapter.key: GoalOuHtAdapter(),
}
