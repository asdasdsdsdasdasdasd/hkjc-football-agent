"""1X2 match result market adapter with competition + team context."""

from __future__ import annotations

from typing import Any

from pipeline.betting.devig import devig_multi_way
from pipeline.betting.load import parse_match_date, parse_score_total
from pipeline.betting.models.score_poisson import ScorelineState, fit_scoreline_model, predict_1x2_probs
from pipeline.betting.types import BetOpportunity, BetOutcome, ModelState


def _parse_score_pair(score: str | None) -> tuple[int, int] | None:
    if not score:
        return None
    parts = score.replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _classify_1x2(score: str | None) -> str | None:
    pair = _parse_score_pair(score)
    if pair is None:
        return None
    home, away = pair
    if home > away:
        return "home"
    if home < away:
        return "away"
    return "draw"


def _selection_side(selection: str) -> str | None:
    sel = selection or ""
    if "主隊勝" in sel:
        return "home"
    if "客隊勝" in sel:
        return "away"
    if sel.strip() == "和":
        return "draw"
    return None


def _fit_1x2(train_matches: list[dict[str, Any]], score_key: str) -> ScorelineState | None:
    return fit_scoreline_model(train_matches, score_key=score_key)


def _predict_1x2(state: ScorelineState, match: dict[str, Any], side: str) -> float:
    return predict_1x2_probs(state, match).get(side, 0.0)


def _market_1x2_probs(opportunities: list[BetOpportunity]) -> dict[str, float] | None:
    odds_by_side = {
        opp.side: opp.decimal_odds
        for opp in opportunities
        if opp.line == "1x2" and opp.side in ("home", "draw", "away")
    }
    if set(odds_by_side) != {"home", "draw", "away"}:
        return None
    return devig_multi_way(odds_by_side)


class Match1x2Adapter:
    key = "match_1x2"
    odds_section = "主客和"
    score_key = "full_time"

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
            selection = entry.get("selection")
            odds = entry.get("odds")
            sel_side = _selection_side(str(selection or ""))
            if sel_side is None or odds is None:
                continue
            out.append(
                BetOpportunity(
                    **base,
                    line="1x2",
                    side=sel_side,
                    decimal_odds=float(odds),
                )
            )
        return out

    def settle(self, opportunity: BetOpportunity, match: dict[str, Any]) -> BetOutcome:
        scores = match.get("scores") or {}
        result = _classify_1x2(scores.get(self.score_key))
        if result is None:
            return BetOutcome.UNKNOWN
        return BetOutcome.WIN if result == opportunity.side else BetOutcome.LOSE

    def training_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [m for m in matches if parse_score_total((m.get("scores") or {}).get(self.score_key)) is not None]

    def fit(self, train_matches: list[dict[str, Any]]) -> ModelState | None:
        model = _fit_1x2(train_matches, self.score_key)
        if model is None:
            return None
        return ModelState(data={"model": model})

    def predict(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float:
        market_probs = _market_1x2_probs(self.extract_opportunities(match))
        if market_probs is not None:
            return market_probs.get(opportunity.side, 0.0)
        model: ScorelineState = state.data["model"]
        return _predict_1x2(model, match, opportunity.side)


class Match1x2HtAdapter(Match1x2Adapter):
    key = "match_1x2_ht"
    odds_section = "半場主客和"
    score_key = "half_time"


MATCH_1X2_ADAPTERS = {
    Match1x2Adapter.key: Match1x2Adapter(),
    Match1x2HtAdapter.key: Match1x2HtAdapter(),
}
