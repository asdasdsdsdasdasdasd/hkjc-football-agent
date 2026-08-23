"""De-vig implied probabilities from betting markets."""

from __future__ import annotations


def devig_multi_way(odds_by_side: dict[str, float]) -> dict[str, float]:
    """Return fair probabilities for an N-way market, preserving side keys."""
    inv: dict[str, float] = {}
    for side, odds in odds_by_side.items():
        if odds > 0:
            inv[side] = 1.0 / odds
    total = sum(inv.values())
    if total <= 0:
        return {side: 0.0 for side in odds_by_side}
    return {side: inv.get(side, 0.0) / total for side in odds_by_side}


def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Return fair probabilities for side A and side B summing to 1."""
    if odds_a <= 0 or odds_b <= 0:
        return 0.0, 0.0
    inv_a = 1.0 / odds_a
    inv_b = 1.0 / odds_b
    total = inv_a + inv_b
    if total <= 0:
        return 0.0, 0.0
    return inv_a / total, inv_b / total


def implied_over_under(over_odds: float | None, under_odds: float | None, side: str) -> float | None:
    if over_odds is None or under_odds is None:
        return None
    p_over, p_under = devig_two_way(over_odds, under_odds)
    if side == "over":
        return p_over
    if side == "under":
        return p_under
    return None


def implied_multi_way(odds_by_side: dict[str, float], side: str) -> float | None:
    if side not in odds_by_side:
        return None
    return devig_multi_way(odds_by_side).get(side)
