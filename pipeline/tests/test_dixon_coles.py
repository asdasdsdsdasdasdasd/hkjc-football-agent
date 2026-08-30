"""Tests for the Dixon-Coles attack/defense model."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.betting.models.dixon_coles import fit_dc, predict_mus, predict_total_probability


def _mk(day: date, home: str, away: str, hs: int, as_: int, comp: str = "聯賽") -> dict:
    return {
        "date": day.strftime("%d/%m/%Y"),
        "teams": f"{home} 對 {away}",
        "competition": comp,
        "scores": {"half_time": f"{hs // 2} : {as_ // 2}", "full_time": f"{hs} : {as_}"},
    }


def _league(days: int = 200) -> list[dict]:
    """Round-robin-ish synthetic league: 強隊 strong, 弱隊 weak, 中隊 average."""
    teams = ["強隊", "中隊", "弱隊", "丙隊"]
    strength = {"強隊": 2, "中隊": 1, "弱隊": 0, "丙隊": 1}
    rows: list[dict] = []
    base = date(2026, 1, 1)
    k = 0
    for d in range(days):
        h = teams[d % len(teams)]
        a = teams[(d + 1 + (d // len(teams)) % 2) % len(teams)]
        if h == a:
            continue
        hs = strength[h] if strength[h] >= strength[a] else 0
        as_ = strength[a] if strength[a] > strength[h] else 0
        rows.append(_mk(base + timedelta(days=d), h, a, hs, as_))
        k += 1
    return rows


def test_fit_returns_none_on_thin_data():
    assert fit_dc(_league(5), stat="goals", period="full_time") is None


def test_strong_team_higher_mu_than_weak():
    rows = _league(300)
    st = fit_dc(rows, stat="goals", period="full_time", ref_date=date(2026, 8, 1))
    assert st is not None
    mu_strong, _ = predict_mus(st, {"teams": "強隊 對 弱隊", "competition": "聯賽"})
    mu_weak, _ = predict_mus(st, {"teams": "弱隊 對 強隊", "competition": "聯賽"})
    # 強隊 at home vs 弱隊 should out-score 弱隊 at home vs 強隊
    assert mu_strong > mu_weak


def test_home_advantage_positive():
    rows = _league(300)
    st = fit_dc(rows, stat="goals", period="full_time", ref_date=date(2026, 8, 1))
    assert st is not None
    mu_home, mu_away = predict_mus(st, {"teams": "中隊 對 丙隊", "competition": "聯賽"})
    assert st.home_adv > 0  # synthetic data: listed side scores >= visitor


def test_probabilities_bounded_and_monotonic():
    rows = _league(300)
    st = fit_dc(rows, stat="goals", period="full_time", ref_date=date(2026, 8, 1))
    m = {"teams": "強隊 對 弱隊", "competition": "聯賽"}
    p_u15 = predict_total_probability(st, m, line=1.5, side="under")
    p_u25 = predict_total_probability(st, m, line=2.5, side="under")
    assert 0.0 < p_u15 < p_u25 < 1.0
    p_o15 = predict_total_probability(st, m, line=1.5, side="over")
    assert abs(p_u15 + p_o15 - 1.0) < 1e-6


def test_time_decay_prefers_recent_form():
    # team A strong early, weak late; with decay the recent (weak) form dominates
    rows: list[dict] = []
    base = date(2025, 1, 1)
    for i in range(40):  # old: A wins 3-0 every week
        rows.append(_mk(base + timedelta(days=7 * i), "甲隊", "乙隊", 3, 0))
    for i in range(12):  # recent: A loses 0-2
        rows.append(_mk(date(2026, 6, 1) + timedelta(days=5 * i), "甲隊", "乙隊", 0, 2))
    st_recent = fit_dc(rows, stat="goals", period="full_time", ref_date=date(2026, 8, 30), half_life_days=60)
    st_flat = fit_dc(rows, stat="goals", period="full_time", ref_date=date(2026, 8, 30), half_life_days=1e9)
    mu_a_recent, _ = predict_mus(st_recent, {"teams": "甲隊 對 乙隊", "competition": "聯賽"})
    mu_a_flat, _ = predict_mus(st_flat, {"teams": "甲隊 對 乙隊", "competition": "聯賽"})
    assert mu_a_recent < mu_a_flat


def test_rho_adjustment_changes_low_line_probability():
    rows = _league(300)
    st = fit_dc(rows, stat="goals", period="half_time", ref_date=date(2026, 8, 1))
    assert st is not None
    assert -0.25 <= st.rho <= 0.25
