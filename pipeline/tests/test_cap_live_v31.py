"""v3.2 live-cut rules: odds, composite, corner line, weekend, max 1/match."""

from __future__ import annotations

import unittest

from pipeline.eval_recent_models import cap_live_v31


def _row(**overrides: object) -> dict:
    row = {
        "match_id": "FB1",
        "bet": "BET",
        "action": "KEEP",
        "market": "goal_ou_ht",
        "side": "under",
        "odds": 1.60,
        "composite_score": 0.25,
        "line": "[0.5]",
        "date": "2026-08-05",  # Wednesday
    }
    row.update(overrides)
    return row


def _ids(rows: list[dict]) -> set[tuple]:
    return {(r["match_id"], r["market"], r.get("line"), r.get("odds")) for r in rows}


class CapLiveV31Tests(unittest.TestCase):
    def test_odds_band(self) -> None:
        low = _row(odds=1.54, match_id="LO")
        lo = _row(odds=1.55, match_id="IN1")
        hi = _row(odds=1.80, match_id="IN2")
        high = _row(odds=1.81, match_id="HI")
        kept = cap_live_v31([low, lo, hi, high])
        self.assertEqual(_ids(kept), {("IN1", "goal_ou_ht", "[0.5]", 1.55), ("IN2", "goal_ou_ht", "[0.5]", 1.80)})

    def test_composite_band(self) -> None:
        low = _row(composite_score=0.09, match_id="LO")
        lo = _row(composite_score=0.10, match_id="IN1")
        hi = _row(composite_score=0.50, match_id="IN2")
        high = _row(composite_score=0.51, match_id="HI")
        kept = cap_live_v31([low, lo, hi, high])
        self.assertEqual(
            _ids(kept),
            {("IN1", "goal_ou_ht", "[0.5]", 1.60), ("IN2", "goal_ou_ht", "[0.5]", 1.60)},
        )

    def test_corner_line_floor(self) -> None:
        below = _row(market="corner_ou_ft", line="[10.0]", match_id="LO")
        floor = _row(market="corner_ou_ft", line="[10.5]", match_id="IN1")
        above = _row(market="corner_ou_ft", line="[11.5]", match_id="IN2")
        kept = cap_live_v31([below, floor, above])
        self.assertEqual(
            _ids(kept),
            {("IN1", "corner_ou_ft", "[10.5]", 1.60), ("IN2", "corner_ou_ft", "[11.5]", 1.60)},
        )

    def test_weekend_excludes_corner_not_goal(self) -> None:
        sat = "2026-08-01"  # Saturday
        sun = "2026-08-02"  # Sunday
        wed = "2026-08-05"
        corner_sat = _row(market="corner_ou_ft", line="[10.5]", date=sat, match_id="CS")
        corner_sun = _row(market="corner_ou_ft", line="[10.5]", date=sun, match_id="CU")
        corner_wed = _row(market="corner_ou_ft", line="[10.5]", date=wed, match_id="CW")
        goal_sat = _row(market="goal_ou_ht", date=sat, match_id="GS")
        goal_wed = _row(market="goal_ou_ht", date=wed, match_id="GW")
        kept = cap_live_v31([corner_sat, corner_sun, corner_wed, goal_sat, goal_wed])
        self.assertEqual(
            {r["match_id"] for r in kept},
            {"CW", "GS", "GW"},
        )

    def test_weekend_parses_iso_datetime(self) -> None:
        corner_sat = _row(
            market="corner_ou_ft",
            line="[10.5]",
            date="2026-08-01T19:00:00",
            match_id="CS",
        )
        self.assertEqual(cap_live_v31([corner_sat]), [])

    def test_max_one_per_match_keeps_highest_composite(self) -> None:
        goal = _row(match_id="FB9", composite_score=0.20, market="goal_ou_ht")
        corner = _row(
            match_id="FB9",
            composite_score=0.40,
            market="corner_ou_ft",
            line="[10.5]",
        )
        kept = cap_live_v31([goal, corner])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["market"], "corner_ou_ft")
        self.assertEqual(kept[0]["composite_score"], 0.40)

    def test_market_and_side_restriction(self) -> None:
        ok_goal = _row(match_id="G")
        ok_corner = _row(match_id="C", market="corner_ou_ft", line="[10.5]")
        rejects = [
            _row(match_id="R1", market="goal_ou_ft"),
            _row(match_id="R2", market="goal_ou_ht", side="over"),
            _row(match_id="R3", market="corner_ou_ht", side="under", line="[5.5]"),
            _row(match_id="R4", market="corner_ou_ft", side="over", line="[10.5]"),
        ]
        kept = cap_live_v31([ok_goal, ok_corner, *rejects])
        self.assertEqual({r["match_id"] for r in kept}, {"G", "C"})

    def test_bet_star_kept_paper_dropped(self) -> None:
        star = _row(bet="BET*", match_id="STAR")
        paper = _row(bet="PAPER", match_id="PAPER")
        pass_row = _row(bet="PASS", match_id="PASS")
        kept = cap_live_v31([star, paper, pass_row])
        self.assertEqual([r["match_id"] for r in kept], ["STAR"])


if __name__ == "__main__":
    unittest.main()
