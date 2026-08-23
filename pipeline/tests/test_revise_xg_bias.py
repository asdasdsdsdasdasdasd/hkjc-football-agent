"""Revision engine xG / form guard tests."""

from __future__ import annotations

import unittest
from datetime import date

from pipeline.revise_recommendations import (
    HT_GOAL_XG_SHARE,
    POLICY_VERSION,
    ScoredLine,
    _form_bias,
    _is_high_odds_goal_over,
    _is_orig_keep_or_flip,
    _is_team_goal_market,
    _paper_reason,
    _passes_market_consistency,
    _xg_bias,
    _xg_expected_total,
)
from pipeline.betting.types import BetOpportunity


def _opp(*, market: str = "goal_ou_ft", side: str = "under", odds: float = 1.70) -> BetOpportunity:
    return BetOpportunity(
        date=date(2026, 7, 24),
        match_id="FBTEST",
        market=market,
        line="[2.5]",
        side=side,
        decimal_odds=odds,
        teams="Home 對 Away",
        competition="",
        over_odds=2.1,
        under_odds=odds,
    )


def _scored(
    *,
    market: str = "goal_ou_ft",
    side: str = "under",
    odds: float = 1.70,
    old_p: float = 0.85,
    market_p: float | None = 0.72,
    market_ev: float = -0.07,
    composite: float = 0.20,
) -> ScoredLine:
    opp = _opp(market=market, side=side, odds=odds)
    return ScoredLine(
        opp=opp,
        old_p=old_p,
        old_ev=old_p * odds - 1,
        revised_p=old_p,
        revised_ev=old_p * odds - 1,
        revised_mu=2.1,
        model_side=side,
        market_p=market_p,
        market_ev=market_ev,
        form_bias=0.0,
        form_depth=5,
        xg_bias=0.0,
        weather_bias=0.0,
        style_bias=0.0,
        composite=composite,
        form_note="",
        xg_note="",
        weather_note="",
        reasons=[],
    )


class ReviseXgBiasTests(unittest.TestCase):
    def test_team_goal_markets_are_identified_for_paper_trade(self) -> None:
        self.assertTrue(_is_team_goal_market("goal_ou_home_ft"))
        self.assertTrue(_is_team_goal_market("goal_ou_away_ht"))
        self.assertFalse(_is_team_goal_market("goal_ou_ft"))
        self.assertFalse(_is_team_goal_market("corner_ou_home_ft"))

    def test_high_odds_goal_overs_are_rejected(self) -> None:
        def opp(market: str, odds: float) -> BetOpportunity:
            return BetOpportunity(
                date=None,
                match_id="FBTEST",
                market=market,
                line="[0.5]",
                side="over",
                decimal_odds=odds,
                teams="Home 對 Away",
                competition="",
                over_odds=odds,
                under_odds=1.8,
            )

        self.assertTrue(_is_high_odds_goal_over(opp("goal_ou_home_ht", 2.20)))
        self.assertTrue(_is_high_odds_goal_over(opp("goal_ou_ft", 2.50)))
        self.assertFalse(_is_high_odds_goal_over(opp("goal_ou_ft", 2.49)))
        self.assertFalse(_is_high_odds_goal_over(opp("corner_ou_ht", 3.8)))

    def test_ht_xg_scales_full_match_bsd(self) -> None:
        self.assertAlmostEqual(_xg_expected_total("goal_ou_ht", 1.26, 1.04), 2.3 * HT_GOAL_XG_SHARE)
        self.assertAlmostEqual(_xg_expected_total("goal_ou_ft", 1.26, 1.04), 2.3)

    def test_fb3024_goal_overs_penalized(self) -> None:
        feat = {"home_xg": 1.26, "away_xg": 1.04}
        ht_bias, _ = _xg_bias(feat, "goal_ou_ht", "[1.5]", "over")
        ft_bias, _ = _xg_bias(feat, "goal_ou_ft", "[2.5]", "over")
        self.assertLess(ht_bias, -0.08)
        self.assertLess(ft_bias, -0.08)

    def test_form_does_not_boost_over_when_xg_below_line(self) -> None:
        form = {"goal_ft_home_match_total_avg": 5.6, "goal_ft_away_match_total_avg": 5.6}
        raw = _form_bias(form, "goal_ou_ft", "[2.5]", "over", xg_effective=None)
        guarded = _form_bias(form, "goal_ou_ft", "[2.5]", "over", xg_effective=2.3)
        self.assertGreater(raw, 0.0)
        self.assertEqual(guarded, 0.0)

    def test_v4_papers_over_corner_and_new(self) -> None:
        self.assertEqual(POLICY_VERSION, "v4")
        self.assertIsNotNone(_paper_reason(_scored(side="over"), is_seeded=True))
        self.assertIsNotNone(_paper_reason(_scored(market="corner_ou_ft"), is_seeded=True))
        self.assertIsNotNone(_paper_reason(_scored(), is_seeded=False, match_seeded=False))
        # Match-total goal under upgrade on a seeded match is live-eligible.
        self.assertIsNone(
            _paper_reason(_scored(market="goal_ou_ft", old_p=0.85, market_p=0.72), is_seeded=False, match_seeded=True)
        )
        self.assertIsNone(_paper_reason(_scored(old_p=0.85, market_p=0.72), is_seeded=True))

    def test_v3_allows_corners_and_overs_live(self) -> None:
        self.assertIsNone(_paper_reason(_scored(market="corner_ou_ft"), is_seeded=True, policy="v3"))
        self.assertIsNone(_paper_reason(_scored(side="over"), is_seeded=True, policy="v3"))
        self.assertIsNotNone(_paper_reason(_scored(market="goal_ou_home_ft"), is_seeded=True, policy="v3"))

    def test_v31_allows_corners_and_unders_live(self) -> None:
        self.assertIsNone(_paper_reason(_scored(market="corner_ou_ft", side="under"), is_seeded=True, policy="v3.1"))
        self.assertIsNone(_paper_reason(_scored(market="goal_ou_ht", side="under"), is_seeded=True, policy="v3.1"))
        self.assertIsNotNone(_paper_reason(_scored(side="over"), is_seeded=True, policy="v3.1"))
        self.assertIsNotNone(_paper_reason(_scored(market="goal_ou_home_ft", side="under"), is_seeded=True, policy="v3.1"))

    def test_market_consistency_rejects_poisson_hallucination(self) -> None:
        halluc = _scored(old_p=0.83, market_p=0.36, market_ev=-0.08)
        ok = _scored(old_p=0.85, market_p=0.72, market_ev=-0.07)
        self.assertFalse(_passes_market_consistency(halluc))
        self.assertTrue(_passes_market_consistency(ok))

    def test_keep_or_flip_detection(self) -> None:
        orig = {("goal_ou_ft", "[2.5]", "over")}
        self.assertTrue(_is_orig_keep_or_flip(("goal_ou_ft", "[2.5]", "over"), orig))
        self.assertTrue(_is_orig_keep_or_flip(("goal_ou_ft", "[2.5]", "under"), orig))
        self.assertFalse(_is_orig_keep_or_flip(("goal_ou_ht", "[0.5]", "under"), orig))


if __name__ == "__main__":
    unittest.main()
