"""Team-specific over/under market adapters (home/away corner & goal)."""

from __future__ import annotations

from typing import Any

from pipeline.betting.devig import implied_over_under
from pipeline.betting.load import parse_match_date, parse_score_total
from pipeline.betting.models.poisson_total import (
    PoissonTotalState,
    expected_over_under_roi,
    fair_over_under_odds,
    fit_poisson_total,
    predict_side_probability,
    predict_team_mu,
)
from pipeline.betting.settlement import settle_over_under
from pipeline.betting.types import BetOpportunity, BetOutcome, ModelState


def team_role_from_market(market: str) -> str | None:
    if "_home_" in market:
        return "home"
    if "_away_" in market:
        return "away"
    return None


def _corner_team_count(match: dict[str, Any], period: str, team_role: str) -> int | None:
    block = (match.get("corners") or {}).get(period) or {}
    val = block.get(team_role)
    return int(val) if val is not None else None


def _goal_team_count(match: dict[str, Any], period: str, team_role: str) -> int | None:
    scores = match.get("scores") or {}
    key = "full_time" if period == "full_time" else "half_time"
    total = parse_score_total(scores.get(key))
    if total is None:
        return None
    parts = (scores.get(key) or "").replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        home, away = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return home if team_role == "home" else away


class _TeamOuAdapter:
    key: str
    odds_section: str
    period: str
    stat: str
    team_role: str

    def _team_count(self, match: dict[str, Any]) -> int | None:
        if self.stat == "corners":
            return _corner_team_count(match, self.period, self.team_role)
        return _goal_team_count(match, self.period, self.team_role)

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
            team = entry.get("team")
            if team is not None and team != self.team_role:
                continue
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
        count = self._team_count(match)
        return settle_over_under(count, opportunity.line, opportunity.side)

    def training_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [m for m in matches if self._team_count(m) is not None]

    def fit(self, train_matches: list[dict[str, Any]]) -> ModelState | None:
        state = fit_poisson_total(train_matches, stat=self.stat, period=self.period)
        if state is None:
            return None
        return ModelState(data={"poisson": state, "team_role": self.team_role})

    def predict(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        p_market = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
        if p_market is not None:
            return p_market
        poisson: PoissonTotalState = state.data["poisson"]
        team_role: str = state.data["team_role"]
        return predict_side_probability(
            poisson,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            team_role=team_role,
        )

    def expected_roi(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        p_market = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
        if p_market is not None:
            return p_market * opportunity.decimal_odds - 1.0
        poisson: PoissonTotalState = state.data["poisson"]
        team_role: str = state.data["team_role"]
        return expected_over_under_roi(
            poisson,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            decimal_odds=opportunity.decimal_odds,
            team_role=team_role,
        )

    def fair_odds(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float | None:
        p_market = implied_over_under(opportunity.over_odds, opportunity.under_odds, opportunity.side)
        if p_market is not None and p_market > 0:
            return 1.0 / p_market
        poisson: PoissonTotalState = state.data["poisson"]
        team_role: str = state.data["team_role"]
        return fair_over_under_odds(
            poisson,
            match,
            line_raw=opportunity.line,
            side=opportunity.side,
            team_role=team_role,
        )


class GoalOuHomeFtAdapter(_TeamOuAdapter):
    key = "goal_ou_home_ft"
    odds_section = "球隊入球大細"
    period = "full_time"
    stat = "goals"
    team_role = "home"


class GoalOuAwayFtAdapter(_TeamOuAdapter):
    key = "goal_ou_away_ft"
    odds_section = "球隊入球大細"
    period = "full_time"
    stat = "goals"
    team_role = "away"


class GoalOuHomeHtAdapter(_TeamOuAdapter):
    key = "goal_ou_home_ht"
    odds_section = "球隊半場入球大細"
    period = "half_time"
    stat = "goals"
    team_role = "home"


class GoalOuAwayHtAdapter(_TeamOuAdapter):
    key = "goal_ou_away_ht"
    odds_section = "球隊半場入球大細"
    period = "half_time"
    stat = "goals"
    team_role = "away"


class CornerOuHomeFtAdapter(_TeamOuAdapter):
    key = "corner_ou_home_ft"
    odds_section = "球隊開出角球大細"
    period = "full_time"
    stat = "corners"
    team_role = "home"


class CornerOuAwayFtAdapter(_TeamOuAdapter):
    key = "corner_ou_away_ft"
    odds_section = "球隊開出角球大細"
    period = "full_time"
    stat = "corners"
    team_role = "away"


class CornerOuHomeHtAdapter(_TeamOuAdapter):
    key = "corner_ou_home_ht"
    odds_section = "球隊半場開出角球大細"
    period = "half_time"
    stat = "corners"
    team_role = "home"


class CornerOuAwayHtAdapter(_TeamOuAdapter):
    key = "corner_ou_away_ht"
    odds_section = "球隊半場開出角球大細"
    period = "half_time"
    stat = "corners"
    team_role = "away"


TEAM_OU_ADAPTERS = {
    GoalOuHomeFtAdapter.key: GoalOuHomeFtAdapter(),
    GoalOuAwayFtAdapter.key: GoalOuAwayFtAdapter(),
    GoalOuHomeHtAdapter.key: GoalOuHomeHtAdapter(),
    GoalOuAwayHtAdapter.key: GoalOuAwayHtAdapter(),
    CornerOuHomeFtAdapter.key: CornerOuHomeFtAdapter(),
    CornerOuAwayFtAdapter.key: CornerOuAwayFtAdapter(),
    CornerOuHomeHtAdapter.key: CornerOuHomeHtAdapter(),
    CornerOuAwayHtAdapter.key: CornerOuAwayHtAdapter(),
}

TEAM_OU_MARKET_KEYS = list(TEAM_OU_ADAPTERS.keys())
