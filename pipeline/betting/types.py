"""Shared types for the betting backtest pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Protocol


class BetOutcome(str, Enum):
    WIN = "win"
    LOSE = "lose"
    PUSH = "push"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BetOpportunity:
    date: date
    match_id: str
    market: str
    line: str
    side: str
    decimal_odds: float
    teams: str
    competition: str | None
    # Raw odds pair for devig diagnostics (over/under markets).
    over_odds: float | None = None
    under_odds: float | None = None


@dataclass(frozen=True)
class BetRecord:
    opportunity: BetOpportunity
    p_model: float
    ev: float
    p_implied: float | None
    edge_vs_close: float | None
    outcome: BetOutcome
    pnl: float
    train_size: int


@dataclass
class ModelState:
    """Opaque fitted state produced by a market adapter."""

    data: dict[str, Any]


class MarketAdapter(Protocol):
    key: str

    def extract_opportunities(self, match: dict[str, Any]) -> list[BetOpportunity]: ...

    def settle(self, opportunity: BetOpportunity, match: dict[str, Any]) -> BetOutcome: ...

    def fit(self, train_matches: list[dict[str, Any]]) -> ModelState | None: ...

    def predict(self, state: ModelState, opportunity: BetOpportunity, match: dict[str, Any]) -> float: ...

    def training_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
