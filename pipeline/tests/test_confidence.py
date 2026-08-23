"""Tests for empirical bet-confidence annotations."""

from __future__ import annotations

import unittest

from pipeline.betting.confidence import build_profile, market_family, wilson_lower_bound


def _row(market: str, outcome: str, odds: float = 2.0) -> dict[str, object]:
    return {"market": market, "outcome": outcome, "odds": odds}


class ConfidenceTests(unittest.TestCase):
    def test_market_family_distinguishes_team_and_match_markets(self) -> None:
        self.assertEqual(market_family("goal_ou_home_ft"), "goal_team")
        self.assertEqual(market_family("corner_ou_away_ht"), "corner_team")
        self.assertEqual(market_family("goal_ou_ft"), "goal_match")

    def test_wilson_bound_penalizes_small_samples(self) -> None:
        self.assertLess(wilson_lower_bound(wins=2, n=2), wilson_lower_bound(wins=16, n=20))

    def test_exact_market_is_used_only_after_minimum_sample(self) -> None:
        rows = [_row("corner_ou_ft", "win") for _ in range(8)]
        rows += [_row("corner_ou_home_ft", "lose") for _ in range(4)]
        rows += [_row("corner_ou_away_ft", "lose") for _ in range(8)]
        profile = build_profile(rows)

        exact = profile.annotate({"market": "corner_ou_ft", "odds": 1.8})
        fallback = profile.annotate({"market": "corner_ou_home_ft", "odds": 1.8})

        self.assertEqual(exact["confidence_group"], "market:corner_ou_ft")
        self.assertEqual(fallback["confidence_group"], "family:corner_team")
        self.assertEqual(fallback["confidence_label"], "UNPROVEN")


if __name__ == "__main__":
    unittest.main()
