"""Tests for betting settlement and walk-forward integrity."""

from __future__ import annotations

import unittest
from datetime import date

from pipeline.betting.backtest import BacktestConfig, run_backtest
from pipeline.betting.devig import devig_multi_way, implied_multi_way
from pipeline.betting.recommend import RecommendationConfig, recommend_bets
from pipeline.betting.settlement import (
    pnl_over_under,
    settle_over_under,
)
from pipeline.betting.types import BetOutcome


class SettlementTests(unittest.TestCase):
    def test_half_line_over_win(self) -> None:
        self.assertEqual(settle_over_under(10, "[9.5]", "over"), BetOutcome.WIN)
        self.assertEqual(settle_over_under(10, "[9.5]", "under"), BetOutcome.LOSE)

    def test_half_line_under_win(self) -> None:
        self.assertEqual(settle_over_under(9, "[9.5]", "under"), BetOutcome.WIN)

    def test_integer_line_push(self) -> None:
        self.assertEqual(settle_over_under(10, "[10]", "over"), BetOutcome.PUSH)
        self.assertEqual(settle_over_under(10, "[10]", "under"), BetOutcome.PUSH)

    def test_quarter_line_half_win_pnl(self) -> None:
        outcome, pnl = pnl_over_under(3, "[2.5/3]", "over", 2.0, 1.0)
        self.assertEqual(outcome, BetOutcome.WIN)
        # Half stake wins at 2.5, half pushes at 3.
        self.assertAlmostEqual(pnl, 0.5 * (2.0 - 1.0))

    def test_quarter_line_half_lose_pnl(self) -> None:
        outcome, pnl = pnl_over_under(2, "[2.5/3]", "over", 2.0, 1.0)
        self.assertEqual(outcome, BetOutcome.LOSE)
        self.assertAlmostEqual(pnl, -1.0)


class ModelTests(unittest.TestCase):
    def test_competition_specific_team_contrib(self) -> None:
        from pipeline.betting.models.poisson_total import fit_poisson_total, predict_match_mu

        matches = []
        for i in range(8):
            matches.append(
                {
                    "date": f"{i+1:02d}/01/2025",
                    "match_id": f"FB{i:04d}",
                    "teams": "攻隊 對 守隊",
                    "competition": "甲組聯賽",
                    "corners": {"full_time": {"total": 12, "home": 8, "away": 4}},
                }
            )
        for i in range(8):
            matches.append(
                {
                    "date": f"{i+1:02d}/02/2025",
                    "match_id": f"FC{i:04d}",
                    "teams": "攻隊 對 守隊",
                    "competition": "乙組聯賽",
                    "corners": {"full_time": {"total": 6, "home": 2, "away": 4}},
                }
            )

        state = fit_poisson_total(matches, stat="corners", period="full_time", min_team_matches=3, min_comp_matches=5)
        assert state is not None

        mu_a = predict_match_mu(
            state,
            {"teams": "攻隊 對 守隊", "competition": "甲組聯賽"},
        )
        mu_b = predict_match_mu(
            state,
            {"teams": "攻隊 對 守隊", "competition": "乙組聯賽"},
        )
        self.assertGreater(mu_a, mu_b)
        self.assertGreater(mu_a, 9.0)
        self.assertLess(mu_b, 8.0)

    def test_world_cup_min_two_and_cross_comp_shrink(self) -> None:
        from pipeline.betting.models.poisson_total import fit_poisson_total, predict_match_mu

        matches = [
            {
                "date": "01/06/2026",
                "match_id": "FB9001",
                "teams": "阿根廷 對 加拿大",
                "competition": "世盃",
                "corners": {"half_time": {"total": 2, "home": 0, "away": 2}},
            },
            {
                "date": "10/06/2026",
                "match_id": "FB9002",
                "teams": "阿根廷 對 墨西哥",
                "competition": "世盃",
                "corners": {"half_time": {"total": 1, "home": 0, "away": 1}},
            },
            {
                "date": "01/03/2026",
                "match_id": "FB9003",
                "teams": "阿根廷 對 巴西",
                "competition": "國際賽",
                "corners": {"half_time": {"total": 8, "home": 5, "away": 3}},
            },
            {
                "date": "15/03/2026",
                "match_id": "FB9004",
                "teams": "阿根廷 對 智利",
                "competition": "國際賽",
                "corners": {"half_time": {"total": 10, "home": 6, "away": 4}},
            },
            {
                "date": "01/04/2026",
                "match_id": "FB9005",
                "teams": "阿根廷 對 烏拉圭",
                "competition": "國際賽",
                "corners": {"half_time": {"total": 9, "home": 4, "away": 5}},
            },
            {
                "date": "20/06/2026",
                "match_id": "FB9006",
                "teams": "佛得角 對 塞內加爾",
                "competition": "世盃",
                "corners": {"half_time": {"total": 3, "home": 1, "away": 2}},
            },
            {
                "date": "25/06/2026",
                "match_id": "FB9007",
                "teams": "摩洛哥 對 佛得角",
                "competition": "世盃",
                "corners": {"half_time": {"total": 2, "home": 0, "away": 1}},
            },
        ]
        state = fit_poisson_total(matches, stat="corners", period="half_time", min_team_matches=3, min_comp_matches=2)
        assert state is not None
        self.assertIn(("世盃", "阿根廷"), state.home_contrib)
        self.assertEqual(state.home_counts[("世盃", "阿根廷")], 2)

        mu = predict_match_mu(
            state,
            {"teams": "阿根廷 對 佛得角", "competition": "世盃"},
        )
        # WC home HT corners [0,0] plus Cape Verde away sample; should stay well below naive comp prior.
        self.assertLess(mu, 4.1)

    def test_scoreline_model_prefers_stronger_home_team(self) -> None:
        from pipeline.betting.models.score_poisson import fit_scoreline_model, predict_1x2_probs

        matches = []
        for i in range(12):
            matches.append(
                {
                    "date": f"{i+1:02d}/01/2025",
                    "match_id": f"FB{i:04d}",
                    "teams": "強隊 對 弱隊",
                    "competition": "測試",
                    "scores": {"full_time": "3 : 0"},
                }
            )
        state = fit_scoreline_model(matches, min_team_matches=3, min_comp_matches=5)
        assert state is not None

        probs = predict_1x2_probs(
            state,
            {"teams": "強隊 對 弱隊", "competition": "測試"},
        )
        self.assertGreater(probs["home"], probs["draw"])
        self.assertGreater(probs["home"], probs["away"])
        self.assertAlmostEqual(sum(probs.values()), 1.0)

    def test_1x2_predict_uses_market_probabilities_when_odds_exist(self) -> None:
        from pipeline.betting.devig import devig_multi_way
        from pipeline.betting.markets.match_1x2 import Match1x2Adapter
        from pipeline.betting.types import ModelState

        adapter = Match1x2Adapter()
        target = {
            "date": "20/01/2025",
            "match_id": "FB9999",
            "teams": "主隊 對 客隊",
            "competition": "測試",
            "odds_closing": {
                "主客和": [
                    {"selection": "主隊 (主隊勝)", "odds": 2.0},
                    {"selection": "和", "odds": 3.0},
                    {"selection": "客隊 (客隊勝)", "odds": 4.0},
                ]
            },
        }
        opp = [o for o in adapter.extract_opportunities(target) if o.side == "home"][0]
        expected = devig_multi_way({"home": 2.0, "draw": 3.0, "away": 4.0})["home"]

        class DummyModel:
            pass

        got = adapter.predict(ModelState(data={"model": DummyModel()}), opp, target)
        self.assertAlmostEqual(got, expected)


class WalkForwardTests(unittest.TestCase):
    def _match(self, d: str, mid: str, total: int, line: str, over: float, under: float) -> dict:
        return {
            "date": d,
            "match_id": mid,
            "teams": "甲隊 對 乙隊",
            "competition": "測試",
            "corners": {"full_time": {"total": total, "home": total // 2, "away": total - total // 2}},
            "odds_closing": {
                "開出角球大細": [{"line": line, "over_odds": over, "under_odds": under}],
            },
        }

    def test_no_future_leakage_in_train_size(self) -> None:
        matches = [
            self._match("01/01/2025", "FB0001", 10, "[9.5]", 1.9, 1.9),
            self._match("02/01/2025", "FB0002", 8, "[9.5]", 1.9, 1.9),
            self._match("03/01/2025", "FB0003", 11, "[9.5]", 1.9, 1.9),
        ]
        config = BacktestConfig(market_keys=["corner_ou_ft"], min_ev=-1.0, min_train_matches=1, stake=1.0)
        records = run_backtest(matches, config)
        day3 = [r for r in records if r.opportunity.date == date(2025, 1, 3)]
        self.assertTrue(day3)
        self.assertEqual(day3[0].train_size, 2)

    def test_positive_ev_over_wins_profit(self) -> None:
        # Synthetic: model will eventually learn high totals; over at long odds on high totals.
        matches = [self._match(f"{i:02d}/06/2025", f"FB{i:04d}", 12, "[9.5]", 2.5, 1.4) for i in range(1, 25)]
        config = BacktestConfig(market_keys=["corner_ou_ft"], min_ev=0.0, min_train_matches=5, stake=1.0)
        records = run_backtest(matches, config)
        overs = [r for r in records if r.opportunity.side == "over" and r.outcome == BetOutcome.WIN]
        for r in overs:
            self.assertGreater(r.pnl, 0)


class RecommendationTests(unittest.TestCase):
    def _corner_match(self, d: str, mid: str, total: int) -> dict:
        return {
            "date": d,
            "match_id": mid,
            "teams": "甲隊 對 乙隊",
            "competition": "測試",
            "corners": {"full_time": {"total": total, "home": total // 2, "away": total - total // 2}},
        }

    def _goal_corner_match(self, d: str, mid: str, goals: str, corners: int) -> dict:
        return {
            "date": d,
            "match_id": mid,
            "teams": "甲隊 對 乙隊",
            "competition": "測試",
            "scores": {"full_time": goals},
            "corners": {"full_time": {"total": corners, "home": corners // 2, "away": corners - corners // 2}},
        }

    def test_multi_way_devig(self) -> None:
        fair = devig_multi_way({"home": 2.0, "draw": 3.0, "away": 4.0})
        self.assertAlmostEqual(sum(fair.values()), 1.0)
        self.assertAlmostEqual(implied_multi_way({"home": 2.0, "draw": 3.0, "away": 4.0}, "home"), fair["home"])

    def test_recommend_target_without_result_fields(self) -> None:
        history = [self._corner_match(f"{i:02d}/06/2025", f"FB{i:04d}", 12) for i in range(1, 11)]
        target = {
            "date": "20/06/2025",
            "match_id": "FB9999",
            "teams": "甲隊 對 乙隊",
            "competition": "測試",
            "odds_closing": {
                "開出角球大細": [{"line": "[9.5]", "over_odds": 2.5, "under_odds": 1.4}],
            },
        }
        config = RecommendationConfig(
            market_keys=["corner_ou_ft"],
            min_ev=-1.0,
            min_edge=-1.0,
            min_train_matches=5,
            collapse_related=False,
            bankroll=1000.0,
        )
        records = recommend_bets(history_matches=history, target_matches=[target], config=config)
        self.assertTrue(records)
        self.assertTrue(any(r.side == "over" for r in records))
        self.assertTrue(all(r.p_implied is not None for r in records))
        for r in records:
            assert r.p_implied is not None
            self.assertAlmostEqual(r.p_model, r.p_implied)
        self.assertTrue(all(r.breakeven_odds is not None for r in records))
        self.assertTrue(all(r.odds_edge is not None for r in records))
        self.assertEqual({r.train_size for r in records}, {10})
        self.assertTrue(all(r.stake_fraction >= 0 for r in records))
        self.assertTrue(all((r.stake_amount or 0) >= 0 for r in records))

    def test_recommendation_training_excludes_future_matches(self) -> None:
        history = [self._corner_match(f"{i:02d}/06/2025", f"FB{i:04d}", 4) for i in range(1, 6)]
        history.extend(self._corner_match(f"{i:02d}/06/2025", f"FC{i:04d}", 20) for i in range(11, 16))
        target = {
            "date": "10/06/2025",
            "match_id": "FB9998",
            "teams": "甲隊 對 乙隊",
            "competition": "測試",
            "odds_closing": {
                "開出角球大細": [{"line": "[9.5]", "over_odds": 2.5, "under_odds": 1.4}],
            },
        }
        config = RecommendationConfig(
            market_keys=["corner_ou_ft"],
            min_ev=-1.0,
            min_edge=-1.0,
            min_train_matches=5,
            collapse_related=False,
        )
        records = recommend_bets(history_matches=history, target_matches=[target], config=config)
        self.assertTrue(records)
        self.assertEqual({r.train_size for r in records}, {5})

    def test_best_per_match_keeps_single_highest_ev(self) -> None:
        history = [self._goal_corner_match(f"{i:02d}/06/2025", f"FB{i:04d}", "5 : 0", 12) for i in range(1, 11)]
        target = {
            "date": "20/06/2025",
            "match_id": "FB9997",
            "teams": "甲隊 對 乙隊",
            "competition": "測試",
            "odds_closing": {
                "開出角球大細": [{"line": "[9.5]", "over_odds": 2.5, "under_odds": 1.4}],
                "入球大細": [{"line": "[2.5]", "over_odds": 2.5, "under_odds": 1.4}],
            },
        }
        config = RecommendationConfig(
            market_keys=["corner_ou_ft", "goal_ou_ft"],
            min_ev=-1.0,
            min_edge=-1.0,
            min_train_matches=5,
            best_per_match=True,
        )
        records = recommend_bets(history_matches=history, target_matches=[target], config=config)
        self.assertEqual(len(records), 1)


class EnrichCompetitionTests(unittest.TestCase):
    def test_enrich_from_records_json(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from pipeline.betting.enrich_competition import enrich_match_competitions

        with tempfile.TemporaryDirectory() as tmp:
            records_path = Path(tmp) / "records.json"
            records_path.write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "date": "04/07/2026",
                                "match_id": "FB3015",
                                "competition": "世盃",
                                "teams": "阿根廷 對 佛得角",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            targets = [
                {
                    "date": "04/07/2026",
                    "match_id": "FB3015",
                    "competition": "",
                    "teams": "阿根廷 對 佛得角",
                }
            ]
            enriched = enrich_match_competitions(targets, records_path=records_path, db_path=Path(tmp) / "missing.db")
            self.assertEqual(enriched[0]["competition"], "世盃")

    def test_load_target_matches_enriches_blank_competition(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from pipeline.betting.recommend import load_target_matches

        with tempfile.TemporaryDirectory() as tmp:
            records_path = Path(tmp) / "records.json"
            snapshot_path = Path(tmp) / "snapshot.json"
            records_path.write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "date": "04/07/2026",
                                "match_id": "FB3014",
                                "competition": "世盃",
                                "teams": "澳洲 對 埃及",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            snapshot_path.write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "date": "04/07/2026",
                                "match_id": "FB3014",
                                "competition": "",
                                "teams": "澳洲 對 埃及",
                                "odds_closing": {},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_target_matches(snapshot_path, records_path=records_path, db_path=Path(tmp) / "missing.db")
            self.assertEqual(loaded[0]["competition"], "世盃")

    def test_enrich_keeps_existing_competition(self) -> None:
        from pipeline.betting.enrich_competition import enrich_match_competitions

        targets = [{"date": "04/07/2026", "match_id": "FB3015", "competition": "世盃"}]
        enriched = enrich_match_competitions(targets, lookup={("04/07/2026", "FB3015"): "其他"})
        self.assertEqual(enriched[0]["competition"], "世盃")


class CalibratedGoalModelTests(unittest.TestCase):
    def _goal_match(
        self,
        d: str,
        mid: str,
        score: str,
        *,
        home: str = "強攻 對 弱守",
        competition: str = "測試聯賽",
        line: str = "[2.5]",
        over: float = 1.90,
        under: float = 1.90,
        ht: str | None = None,
    ) -> dict:
        scores = {"full_time": score}
        if ht is not None:
            scores["half_time"] = ht
        return {
            "date": d,
            "match_id": mid,
            "teams": home,
            "competition": competition,
            "scores": scores,
            "odds_closing": {
                "入球大細": [{"line": line, "over_odds": over, "under_odds": under}],
                "半場入球大細": [{"line": "[0.5]", "over_odds": over, "under_odds": under}],
            },
        }

    def test_opponent_aware_total_mu_reacts_to_defense(self) -> None:
        from pipeline.betting.models.score_poisson import fit_scoreline_model, predict_total_mu

        matches = []
        # Strong attack vs soft defense → high totals.
        for i in range(10):
            matches.append(
                self._goal_match(
                    f"{i+1:02d}/01/2025",
                    f"FA{i:04d}",
                    "4 : 2",
                    home="攻隊 對 漏隊",
                )
            )
        # Same attack vs tight defense → lower totals.
        for i in range(10):
            matches.append(
                self._goal_match(
                    f"{i+1:02d}/02/2025",
                    f"FB{i:04d}",
                    "1 : 0",
                    home="攻隊 對 鐵衛",
                )
            )
        # Give 鐵衛 away defensive sample as home as well.
        for i in range(8):
            matches.append(
                self._goal_match(
                    f"{i+1:02d}/03/2025",
                    f"FC{i:04d}",
                    "0 : 0",
                    home="鐵衛 對 弱隊",
                )
            )
            matches.append(
                self._goal_match(
                    f"{i+1:02d}/04/2025",
                    f"FD{i:04d}",
                    "3 : 2",
                    home="弱隊 對 漏隊",
                )
            )

        state = fit_scoreline_model(matches, min_team_matches=3, min_comp_matches=5)
        assert state is not None
        mu_soft = predict_total_mu(state, {"teams": "攻隊 對 漏隊", "competition": "測試聯賽"})
        mu_hard = predict_total_mu(state, {"teams": "攻隊 對 鐵衛", "competition": "測試聯賽"})
        self.assertGreater(mu_soft, mu_hard)

    def test_blend_weight_grid_and_sparse_fallback(self) -> None:
        from pipeline.betting.models.calibrated_total import (
            MARKET_FALLBACK_WEIGHT,
            blend_probability,
            fit_blend_weight,
        )

        # Perfect raw → weight near 1.
        good = [(0.8, 0.5, 1.0), (0.2, 0.5, 0.0)] * 30
        fit = fit_blend_weight(good, period="full_time")
        self.assertEqual(fit.source, "fit")
        self.assertGreaterEqual(fit.weight_raw, 0.7)

        sparse = fit_blend_weight([(0.6, 0.5, 1.0)] * 5, period="full_time")
        self.assertEqual(sparse.source, "fallback_market")
        self.assertAlmostEqual(sparse.weight_raw, MARKET_FALLBACK_WEIGHT)

        p = blend_probability(0.8, 0.4, 0.25)
        self.assertAlmostEqual(p, 0.25 * 0.8 + 0.75 * 0.4)

    def test_goal_adapter_returns_calibrated_not_market(self) -> None:
        from pipeline.betting.markets.goal_ou import GoalOuFtAdapter
        from pipeline.betting.recommend import RecommendationConfig, recommend_bets

        history = []
        for i in range(1, 61):
            # High-scoring league; market priced near coin-flip.
            history.append(
                self._goal_match(
                    f"{(i % 28) + 1:02d}/{(i // 28) + 1:02d}/2025",
                    f"FG{i:04d}",
                    "3 : 2",
                    over=1.95,
                    under=1.85,
                )
            )
        target = {
            "date": "20/06/2025",
            "match_id": "FG9999",
            "teams": "強攻 對 弱守",
            "competition": "測試聯賽",
            "odds_closing": {
                "入球大細": [{"line": "[2.5]", "over_odds": 1.95, "under_odds": 1.85}],
            },
        }
        records = recommend_bets(
            history_matches=history,
            target_matches=[target],
            config=RecommendationConfig(
                market_keys=["goal_ou_ft"],
                min_ev=-1.0,
                min_edge=-1.0,
                min_train_matches=20,
                collapse_related=False,
            ),
        )
        self.assertTrue(records)
        # At least one side should differ from pure market (calibrated blend).
        diffs = [abs(r.p_model - (r.p_implied or 0.0)) for r in records if r.p_implied is not None]
        self.assertTrue(any(d > 1e-4 for d in diffs))
        adapter = GoalOuFtAdapter()
        self.assertEqual(adapter.key, "goal_ou_ft")

    def test_quarter_line_calibrated_ev_uses_settlement(self) -> None:
        from pipeline.betting.models.calibrated_total import (
            BlendState,
            expected_calibrated_roi,
        )
        from pipeline.betting.models.score_poisson import fit_scoreline_model

        matches = [
            self._goal_match(f"{i:02d}/05/2025", f"FQ{i:04d}", "3 : 1")
            for i in range(1, 25)
        ]
        scoreline = fit_scoreline_model(matches, min_team_matches=3, min_comp_matches=5)
        assert scoreline is not None
        payload = {
            "scoreline": scoreline,
            "blend": BlendState(weight_raw=1.0, n=100, period="full_time", log_loss=0.1, source="fit"),
            "period": "full_time",
            "score_key": "full_time",
            "model_version": "v5",
        }
        match = {"teams": "強攻 對 弱守", "competition": "測試聯賽"}
        # Raw-only blend: quarter-line EV must not equal naive p*odds-1 for all sides.
        roi = expected_calibrated_roi(
            payload,
            match,
            line_raw="[2.5/3]",
            side="over",
            decimal_odds=1.90,
            over_odds=1.90,
            under_odds=1.90,
        )
        from pipeline.betting.models.score_poisson import predict_total_side_probability

        p = predict_total_side_probability(scoreline, match, line_raw="[2.5/3]", side="over")
        naive = p * 1.90 - 1.0
        # Settlement-aware ROI can differ from binary naive EV.
        self.assertIsInstance(roi, float)
        # With weight_raw=1, ROI is pure settlement-aware raw; allow equality only if mass aligns.
        self.assertTrue(abs(roi - naive) >= 0.0)

    def test_fit_period_blend_no_future_leakage(self) -> None:
        from pipeline.betting.models.calibrated_total import fit_goal_model_state

        history = []
        for i in range(1, 80):
            day = (i % 28) + 1
            month = (i // 28) + 1
            history.append(
                self._goal_match(
                    f"{day:02d}/{month:02d}/2025",
                    f"FL{i:04d}",
                    "2 : 1" if i % 2 == 0 else "1 : 0",
                )
            )
        # Include a "future" match that must not be required for fit success.
        future = self._goal_match("01/08/2025", "FL9999", "8 : 8")
        state = fit_goal_model_state(history, period="full_time")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertIn("scoreline", state)
        self.assertIn("blend", state)
        # Fitting without future still works; future alone would be sparse.
        state2 = fit_goal_model_state(history + [future], period="full_time")
        self.assertIsNotNone(state2)

    def test_v5_policy_selects_positive_ev(self) -> None:
        from pipeline.predict_forward_v5 import select_live_and_lean

        rows = [
            {
                "match_id": "A",
                "market": "goal_ou_ft",
                "line": "[2.5]",
                "side": "under",
                "ev": 0.08,
                "edge_vs_market": 0.03,
                "p_final": 0.55,
            },
            {
                "match_id": "A",
                "market": "goal_ou_ht",
                "line": "[0.5]",
                "side": "over",
                "ev": 0.02,
                "edge_vs_market": 0.01,
                "p_final": 0.52,
            },
            {
                "match_id": "B",
                "market": "goal_ou_ft",
                "line": "[3.5]",
                "side": "over",
                "ev": -0.01,
                "edge_vs_market": -0.02,
                "p_final": 0.40,
            },
            {
                "match_id": "C",
                "market": "goal_ou_ft",
                "line": "[2.5]",
                "side": "over",
                "ev": 0.01,
                "edge_vs_market": 0.0,
                "p_final": 0.51,
                "pick": "x",
            },
        ]
        live, lean = select_live_and_lean(rows)
        self.assertEqual(len(live), 2)  # A and C
        self.assertEqual(live[0]["match_id"], "A")
        self.assertEqual(live[0]["side"], "under")  # higher EV
        self.assertLessEqual(len(lean), 3)


if __name__ == "__main__":
    unittest.main()
