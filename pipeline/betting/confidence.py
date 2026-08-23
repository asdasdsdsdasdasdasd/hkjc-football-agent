"""Empirical confidence metrics for recommended football bets.

Model probability is not confidence.  Confidence here means: how often did
previous settled picks in the same market family win, how much evidence is
there, and does a conservative lower bound clear this line's break-even rate?
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

MIN_EXACT_SAMPLE = 8
MIN_FAMILY_SAMPLE = 12
PRIOR_STRENGTH = 12
ONE_SIDED_Z = 1.645  # 90% Wilson lower confidence bound.


def market_family(market: str) -> str:
    """Return a stable, coarse group with enough rows to measure."""
    stat = "corner" if "corner" in market else "goal" if "goal" in market else "other"
    scope = "team" if "_home_" in market or "_away_" in market else "match"
    return f"{stat}_{scope}"


def _settled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("outcome") in {"win", "lose"}]


def _summary(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    wins = sum(1 for row in rows if row["outcome"] == "win")
    n = len(rows)
    return {"n": n, "wins": wins, "win_rate": wins / n if n else 0.0}


def wilson_lower_bound(*, wins: int, n: int, z: float = ONE_SIDED_Z) -> float:
    """One-sided Wilson lower bound; returns zero where no evidence exists."""
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return max(0.0, (centre - spread) / (1 + z2 / n))


class ConfidenceProfile:
    """Historical outcome summaries used to annotate a new recommendation."""

    def __init__(self, settled_rows: list[dict[str, Any]]) -> None:
        self.settled_rows = _settled(settled_rows)
        self.overall = _summary(self.settled_rows)
        self._exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._family: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.settled_rows:
            market = str(row.get("market") or "")
            if not market:
                continue
            self._exact[market].append(row)
            self._family[market_family(market)].append(row)

    def _reference_group(self, market: str) -> tuple[str, list[dict[str, Any]]]:
        exact = self._exact.get(market, [])
        if len(exact) >= MIN_EXACT_SAMPLE:
            return f"market:{market}", exact
        family = self._family.get(market_family(market), [])
        if len(family) >= MIN_FAMILY_SAMPLE:
            return f"family:{market_family(market)}", family
        return "overall", self.settled_rows

    def annotate(self, bet: dict[str, Any]) -> dict[str, Any]:
        """Return confidence fields without changing the supplied bet."""
        market = str(bet.get("market") or "")
        group, rows = self._reference_group(market)
        summary = _summary(rows)
        n = int(summary["n"])
        wins = int(summary["wins"])
        empirical = float(summary["win_rate"])
        overall_rate = float(self.overall["win_rate"])
        smoothed = (wins + PRIOR_STRENGTH * overall_rate) / (n + PRIOR_STRENGTH) if n else overall_rate
        lower = wilson_lower_bound(wins=wins, n=n)
        odds = float(bet.get("odds") or 0)
        breakeven = 1 / odds if odds > 1 else 1.0
        edge = lower - breakeven

        # Overall history may describe corners while this bet is a team-goal
        # line. Never call that confidence: it is only a prior for smoothing.
        # HIGH requires evidence from the exact market or its market family.
        has_comparable_group = group != "overall"
        if has_comparable_group and n >= 15 and edge > 0:
            label = "HIGH"
        elif has_comparable_group and n >= 8 and smoothed > breakeven:
            label = "MEDIUM"
        else:
            label = "UNPROVEN"

        return {
            "confidence_group": group,
            "confidence_sample_n": n,
            "confidence_wins": wins,
            "confidence_hit_rate": round(empirical, 4),
            "confidence_smoothed_rate": round(smoothed, 4),
            "confidence_lower_bound": round(lower, 4),
            "confidence_break_even": round(breakeven, 4),
            "confidence_edge": round(edge, 4),
            "confidence_label": label,
        }


def build_profile(rows: list[dict[str, Any]]) -> ConfidenceProfile:
    return ConfidenceProfile(rows)
