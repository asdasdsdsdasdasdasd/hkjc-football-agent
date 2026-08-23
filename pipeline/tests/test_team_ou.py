"""Team over/under market adapter and parser tests."""

from __future__ import annotations

import unittest

from pipeline.betting.markets.team_ou import CornerOuHomeFtAdapter, GoalOuAwayFtAdapter, GoalOuHomeFtAdapter
from pipeline.betting.models.poisson_total import fit_poisson_total, predict_team_mu
from pipeline.betting.types import BetOutcome, BetOpportunity
from pipeline.parsers import parse_odds_sections
from datetime import date


class TeamOuAdapterTests(unittest.TestCase):
    def test_extract_home_and_away_lines_separately(self) -> None:
        match = {
            "date": "08/07/2026",
            "match_id": "FB9000",
            "teams": "主隊 對 客隊",
            "odds_closing": {
                "球隊入球大細": [
                    {"team": "home", "line": "[1.5]", "over_odds": 1.9, "under_odds": 1.9},
                    {"team": "away", "line": "[0.5]", "over_odds": 2.1, "under_odds": 1.7},
                ]
            },
        }
        home = GoalOuHomeFtAdapter()
        away = GoalOuAwayFtAdapter()
        home_opps = home.extract_opportunities(match)
        away_opps = away.extract_opportunities(match)
        self.assertEqual(len(home_opps), 2)
        self.assertEqual(len(away_opps), 2)
        self.assertEqual(home_opps[0].line, "[1.5]")
        self.assertEqual(away_opps[0].line, "[0.5]")

    def test_settle_home_corner_count(self) -> None:
        adapter = CornerOuHomeFtAdapter()
        opp = BetOpportunity(
            date=date(2026, 7, 8),
            match_id="FB9000",
            market="corner_ou_home_ft",
            line="[4.5]",
            side="over",
            decimal_odds=1.9,
            teams="主隊 對 客隊",
            competition="測試",
            over_odds=1.9,
            under_odds=1.9,
        )
        match = {
            "corners": {"full_time": {"total": 9, "home": 6, "away": 3}},
        }
        self.assertEqual(adapter.settle(opp, match), BetOutcome.WIN)

    def test_predict_team_mu_uses_single_team_rate(self) -> None:
        matches = []
        for i in range(6):
            matches.append(
                {
                    "date": f"{i+1:02d}/06/2026",
                    "match_id": f"FB{i:04d}",
                    "teams": "強隊 對 弱隊",
                    "competition": "甲組聯賽",
                    "corners": {"full_time": {"total": 10, "home": 8, "away": 2}},
                }
            )
        state = fit_poisson_total(matches, stat="corners", period="full_time", min_team_matches=2, min_comp_matches=2)
        assert state is not None
        mu_home = predict_team_mu(state, {"teams": "強隊 對 弱隊", "competition": "甲組聯賽"}, team_role="home")
        mu_away = predict_team_mu(state, {"teams": "強隊 對 弱隊", "competition": "甲組聯賽"}, team_role="away")
        self.assertGreater(mu_home, mu_away)


class TeamOuParserTests(unittest.TestCase):
    def test_parse_team_goal_section(self) -> None:
        text = """
球賽編號: FB9000
最後賠率
更新時間
球隊入球大細
主隊
球數
大
細
[1.5]
1.90
1.90
客隊
球數
大
細
[0.5]
2.10
1.70
詳細賽果
"""
        odds = parse_odds_sections(text)
        rows = odds["球隊入球大細"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["team"], "home")
        self.assertEqual(rows[1]["team"], "away")
        self.assertEqual(rows[0]["line"], "[1.5]")


if __name__ == "__main__":
    unittest.main()
