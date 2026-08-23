"""Poisson μ blending with BSD xG."""

from __future__ import annotations

import unittest

from pipeline.betting.models.poisson_total import (
    HT_GOAL_XG_SHARE,
    fit_poisson_total,
    predict_match_mu,
    predict_side_probability,
)


class PoissonXgBlendTests(unittest.TestCase):
    def test_xg_caps_inflated_wc_mu(self) -> None:
        matches = []
        for i in range(6):
            matches.append(
                {
                    "date": f"{i+1:02d}/06/2026",
                    "match_id": f"FB{i:04d}",
                    "teams": "攻隊 對 守隊",
                    "competition": "世盃",
                    "scores": {"full_time": "4 : 4", "half_time": "2 : 2"},
                }
            )
        state = fit_poisson_total(matches, stat="goals", period="full_time", min_team_matches=2, min_comp_matches=2)
        assert state is not None

        match = {
            "teams": "攻隊 對 守隊",
            "competition": "世盃",
            "external_features": {"home_xg": 1.26, "away_xg": 1.04},
        }
        mu_raw = predict_match_mu(state, {**match, "external_features": {}})
        mu_blend = predict_match_mu(state, match)
        xg_total = 1.26 + 1.04
        self.assertGreater(mu_raw, xg_total * 1.5)
        self.assertLessEqual(mu_blend, xg_total * 1.21)
        self.assertGreater(mu_blend, xg_total * 0.9)

    def test_ht_xg_scales_for_half_time_goals(self) -> None:
        matches = [
            {
                "date": "01/07/2026",
                "match_id": "FB0001",
                "teams": "A 對 B",
                "competition": "世盃",
                "scores": {"half_time": "1 : 0", "full_time": "2 : 0"},
            }
        ]
        state = fit_poisson_total(matches, stat="goals", period="half_time", min_team_matches=1, min_comp_matches=1)
        assert state is not None
        match = {
            "teams": "A 對 B",
            "competition": "世盃",
            "external_features": {"home_xg": 2.0, "away_xg": 2.0},
        }
        mu = predict_match_mu(state, match)
        self.assertLessEqual(mu, 4.0 * HT_GOAL_XG_SHARE * 1.21 + 0.05)

    def test_over_probability_drops_when_xg_below_line(self) -> None:
        matches = []
        for i in range(8):
            matches.append(
                {
                    "date": f"{i+1:02d}/06/2026",
                    "match_id": f"FB{i:04d}",
                    "teams": "攻隊 對 守隊",
                    "competition": "世盃",
                    "scores": {"full_time": "3 : 3", "half_time": "2 : 1"},
                }
            )
        state = fit_poisson_total(matches, stat="goals", period="full_time", min_team_matches=2, min_comp_matches=2)
        assert state is not None
        base = {"teams": "攻隊 對 守隊", "competition": "世盃"}
        p_no_xg = predict_side_probability(state, base, line_raw="[2.5]", side="over")
        p_xg = predict_side_probability(
            state,
            {**base, "external_features": {"home_xg": 1.26, "away_xg": 1.04}},
            line_raw="[2.5]",
            side="over",
        )
        self.assertGreater(p_no_xg, p_xg)


if __name__ == "__main__":
    unittest.main()
