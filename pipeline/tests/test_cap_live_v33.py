"""v3.3 live-cut rules: weekday corner FT under only, composite floor variants."""

from __future__ import annotations

import unittest

from pipeline.eval_recent_models import cap_live_v33


def _row(**overrides: object) -> dict:
    row = {
        "match_id": "FB1",
        "bet": "BET",
        "action": "KEEP",
        "market": "corner_ou_ft",
        "side": "under",
        "odds": 1.60,
        "composite_score": 0.35,
        "line": "[10.5]",
        "date": "2026-08-05",  # Wednesday
    }
    row.update(overrides)
    return row


class CapLiveV33Tests(unittest.TestCase):
    def test_goal_markets_dropped(self) -> None:
        goal = _row(market="goal_ou_ht", match_id="G")
        corner = _row(match_id="C")
        kept = cap_live_v33([goal, corner])
        self.assertEqual([r["match_id"] for r in kept], ["C"])

    def test_comp_floor_030(self) -> None:
        low = _row(composite_score=0.29, match_id="LO")
        edge = _row(composite_score=0.30, match_id="IN1")
        hi = _row(composite_score=0.50, match_id="IN2")
        high = _row(composite_score=0.51, match_id="HI")
        kept = cap_live_v33([low, edge, hi, high], comp_min=0.30)
        self.assertEqual({r["match_id"] for r in kept}, {"IN1", "IN2"})

    def test_comp_floor_020(self) -> None:
        low = _row(composite_score=0.19, match_id="LO")
        edge = _row(composite_score=0.20, match_id="IN1")
        mid = _row(composite_score=0.29, match_id="IN2")
        kept = cap_live_v33([low, edge, mid], comp_min=0.20)
        self.assertEqual({r["match_id"] for r in kept}, {"IN1", "IN2"})

    def test_odds_band_and_line(self) -> None:
        bad_odds = _row(odds=1.54, match_id="O1")
        bad_odds2 = _row(odds=1.81, match_id="O2")
        bad_line = _row(line="[10.0]", match_id="L1")
        ok = _row(match_id="OK")
        kept = cap_live_v33([bad_odds, bad_odds2, bad_line, ok])
        self.assertEqual([r["match_id"] for r in kept], ["OK"])

    def test_weekend_excluded(self) -> None:
        sat = _row(date="2026-08-01", match_id="SAT")
        sun = _row(date="2026-08-02", match_id="SUN")
        wed = _row(date="2026-08-05", match_id="WED")
        kept = cap_live_v33([sat, sun, wed])
        self.assertEqual([r["match_id"] for r in kept], ["WED"])

    def test_max_one_per_match(self) -> None:
        a = _row(match_id="FB9", composite_score=0.31, line="[10.5]")
        b = _row(match_id="FB9", composite_score=0.45, line="[11.5]")
        kept = cap_live_v33([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["composite_score"], 0.45)

    def test_bet_star_kept_paper_dropped(self) -> None:
        star = _row(bet="BET*", match_id="STAR")
        paper = _row(bet="PAPER", match_id="PAPER")
        kept = cap_live_v33([star, paper])
        self.assertEqual([r["match_id"] for r in kept], ["STAR"])


if __name__ == "__main__":
    unittest.main()
