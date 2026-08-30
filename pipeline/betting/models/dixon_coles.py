"""Dixon-Coles style attack/defense Poisson model with exponential time decay.

Replaces the raw team-mean Poisson (poisson_total.py) for probability
estimation.  Structural differences:

  * opponent-adjusted: mu_home = exp(intercept + att[h] + def[a] + home_adv
    + comp_base) — facing a leaky defense raises the expectation, instead of
    adding two raw team averages (which double-counts the league mean and
    ignores the matchup entirely)
  * exponential time decay: recent matches weigh more; half-life 60d chosen
    on a validation window (2026-05-01..07-08), not the test window
  * team ratings are global across competitions (cups reuse league ratings);
    competition enters only as a baseline offset, shrunk by sample size
  * Dixon-Coles tau low-score adjustment fitted for goals (rho ~ 0.01 on
    this data — negligible, but fitted rather than assumed)

Fitted by weighted maximum likelihood (L-BFGS) with a small ridge penalty on
ratings — the Poisson log-likelihood is concave in the log-means, so this is
globally convergent and stable.  Out-of-sample (07-24..08-28, closing odds):
log-loss beats poisson_total on HT goals (0.6642 vs 0.6720) and FT corners
(0.7050 vs 0.7160); market still beats both.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from scipy.optimize import minimize

from pipeline.betting.load import parse_match_date, split_teams
from pipeline.betting.models.poisson_total import _poisson_pmf, prob_over, prob_under

DEFAULT_HALF_LIFE_DAYS = 60.0  # validated on 2026-05-01..07-08
RIDGE_LAMBDA = 1.0  # prior strength in weighted-match units, fixed a priori
COMP_SHRINK_N = 20.0


@dataclass
class DCState:
    attack: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    team_weight: dict[str, float] = field(default_factory=dict)
    intercept: float = 0.0
    home_adv: float = 0.0
    comp_base: dict[str, float] = field(default_factory=dict)
    global_mean: float = 2.6
    rho: float = 0.0  # Dixon-Coles low-score correlation (goals only)
    stat: str = "goals"
    period: str = "full_time"
    n_matches: int = 0


def _split(match: dict[str, Any], stat: str, period: str) -> tuple[int, int] | None:
    if stat == "corners":
        block = (match.get("corners") or {}).get(period) or {}
        home, away = block.get("home"), block.get("away")
        if home is None or away is None:
            return None
        return int(home), int(away)
    score = (match.get("scores") or {}).get(period)
    if not score:
        return None
    parts = str(score).replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def fit_dc(
    matches: list[dict[str, Any]],
    *,
    stat: str = "goals",
    period: str = "full_time",
    ref_date: date | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    iterations: int | None = None,
) -> DCState | None:
    rows: list[tuple[str, str, str, float, int, int]] = []
    for m in matches:
        pair = split_teams(m.get("teams") or "")
        if pair is None:
            continue
        sp = _split(m, stat, period)
        if sp is None:
            continue
        try:
            d = parse_match_date(m["date"])
        except Exception:
            continue
        if ref_date is not None:
            age = (ref_date - d).days
            if age < 0:
                continue
            w = 0.5 ** (age / half_life_days)
        else:
            w = 1.0
        comp = (m.get("competition") or "").strip() or "未知"
        rows.append((pair[0], pair[1], comp, w, sp[0], sp[1]))
    if len(rows) < 50:
        return None

    gw_sum = sum(w * (gh + ga) for _, _, _, w, gh, ga in rows)
    gw_n = sum(w for _, _, _, w, _, _ in rows)
    global_mean = gw_sum / gw_n
    comp_sum: dict[str, float] = {}
    comp_n: dict[str, float] = {}
    for _, _, comp, w, gh, ga in rows:
        comp_sum[comp] = comp_sum.get(comp, 0.0) + w * (gh + ga)
        comp_n[comp] = comp_n.get(comp, 0.0) + w
    comp_base: dict[str, float] = {}
    for c, s in comp_sum.items():
        n = comp_n[c]
        raw = math.log((s / n) / global_mean) if n > 0 and s > 0 else 0.0
        comp_base[c] = raw * (n / (n + COMP_SHRINK_N))

    teams = sorted({h for h, *_ in rows} | {a for _, a, *_ in rows})
    tidx = {t: i for i, t in enumerate(teams)}
    n_teams = len(teams)
    h_idx = np.array([tidx[r[0]] for r in rows])
    a_idx = np.array([tidx[r[1]] for r in rows])
    base = np.array([comp_base[r[2]] for r in rows])
    w_arr = np.array([r[3] for r in rows])
    gh_arr = np.array([r[4] for r in rows], dtype=float)
    ga_arr = np.array([r[5] for r in rows], dtype=float)
    team_w = {t: 0.0 for t in teams}
    for h, a, _, w, _, _ in rows:
        team_w[h] += w
        team_w[a] += w

    def neg_ll(theta: np.ndarray) -> tuple[float, np.ndarray]:
        att = theta[:n_teams]
        dfn = theta[n_teams : 2 * n_teams]
        att = att - att.mean()
        dfn = dfn - dfn.mean()
        intercept = theta[-2]
        home_adv = theta[-1]
        eta_h = intercept + att[h_idx] + dfn[a_idx] + home_adv + base
        eta_a = intercept + att[a_idx] + dfn[h_idx] + base
        mu_h = np.exp(np.clip(eta_h, -12, 12))
        mu_a = np.exp(np.clip(eta_a, -12, 12))
        ll = np.sum(w_arr * (gh_arr * eta_h - mu_h)) + np.sum(w_arr * (ga_arr * eta_a - mu_a))
        pen = RIDGE_LAMBDA * 0.5 * (np.sum(att * att) + np.sum(dfn * dfn))
        # gradients
        res_h = w_arr * (gh_arr - mu_h)  # dLL/d eta_h
        res_a = w_arr * (ga_arr - mu_a)
        g_att = np.bincount(h_idx, weights=res_h, minlength=n_teams) + np.bincount(a_idx, weights=res_a, minlength=n_teams)
        g_def = np.bincount(a_idx, weights=res_h, minlength=n_teams) + np.bincount(h_idx, weights=res_a, minlength=n_teams)
        g_att -= RIDGE_LAMBDA * att
        g_def -= RIDGE_LAMBDA * dfn
        g_inter = float(np.sum(res_h) + np.sum(res_a))
        g_ha = float(np.sum(res_h))
        grad = np.concatenate([g_att, g_def, [g_inter, g_ha]])
        return -(ll - pen), -grad

    theta0 = np.zeros(2 * n_teams + 2)
    theta0[-2] = math.log(max(global_mean / 2.0, 0.05))
    theta0[-1] = 0.2
    res = minimize(
        neg_ll,
        theta0,
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": 500, "ftol": 1e-10},
    )
    theta = res.x
    att = theta[:n_teams]
    dfn = theta[n_teams : 2 * n_teams]
    att = att - att.mean()
    dfn = dfn - dfn.mean()
    intercept = float(theta[-2])
    home_adv = float(theta[-1])

    state = DCState(
        attack={t: float(att[i]) for i, t in enumerate(teams)},
        defense={t: float(dfn[i]) for i, t in enumerate(teams)},
        team_weight=team_w,
        intercept=intercept,
        home_adv=home_adv,
        comp_base=comp_base,
        global_mean=global_mean,
        stat=stat,
        period=period,
        n_matches=len(rows),
    )

    if stat == "goals":
        state.rho = _fit_rho(state, rows)
    return state


def _tau(x: int, y: int, mu_h: float, mu_a: float, rho: float) -> float:
    """Dixon-Coles low-score adjustment factor."""
    if x == 0 and y == 0:
        return 1.0 - mu_h * mu_a * rho
    if x == 0 and y == 1:
        return 1.0 + mu_h * rho
    if x == 1 and y == 0:
        return 1.0 + mu_a * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _fit_rho(state: DCState, rows: list[tuple[str, str, str, float, int, int]]) -> float:
    """1-D MLE for rho given the fitted mus (standard two-stage DC fit)."""
    from scipy.optimize import minimize_scalar

    cells: list[tuple[float, float, float, float]] = []  # w, mu_h, mu_a, log poi part
    for h, a, comp, w, gh, ga in rows:
        if gh > 1 or ga > 1:
            continue
        mus = predict_mus(state, {"teams": f"{h} 對 {a}", "competition": comp})
        if mus is None:
            continue
        mu_h, mu_a = mus
        log_p = -mu_h - mu_a + (gh * math.log(mu_h) if gh else 0.0) + (ga * math.log(mu_a) if ga else 0.0)
        cells.append((w, mu_h, mu_a, log_p, gh, ga))

    def neg(rho: float) -> float:
        tot = 0.0
        for w, mu_h, mu_a, log_p, gh, ga in cells:
            tau = _tau(gh, ga, mu_h, mu_a, rho)
            if tau <= 0:
                return 1e9
            tot += w * (math.log(tau) + log_p)
        return -tot

    res = minimize_scalar(neg, bounds=(-0.25, 0.25), method="bounded")
    return float(res.x)


def predict_mus(state: DCState, match: dict[str, Any]) -> tuple[float, float] | None:
    """(mu_home, mu_away) for the fitted stat/period."""
    pair = split_teams(match.get("teams") or "")
    comp = (match.get("competition") or "").strip() or "未知"
    base = state.comp_base.get(comp, 0.0)
    if pair is None:
        half = max(state.global_mean / 2.0, 0.01)
        return half, half
    home, away = pair
    att_h = state.attack.get(home, 0.0)
    def_h = state.defense.get(home, 0.0)
    att_a = state.attack.get(away, 0.0)
    def_a = state.defense.get(away, 0.0)
    mu_h = math.exp(state.intercept + att_h + def_a + state.home_adv + base)
    mu_a = math.exp(state.intercept + att_a + def_h + base)
    return max(mu_h, 0.01), max(mu_a, 0.01)


def predict_total_mu(state: DCState, match: dict[str, Any]) -> float:
    mus = predict_mus(state, match)
    if mus is None:
        return state.global_mean
    return mus[0] + mus[1]


def predict_total_probability(state: DCState, match: dict[str, Any], *, line: float, side: str) -> float:
    mus = predict_mus(state, match)
    mu = mus[0] + mus[1]
    p_under = prob_under(mu, line)
    if state.stat == "goals" and state.rho != 0.0 and mus is not None:
        # tau-correct the four low cells that fall under the line
        mu_h, mu_a = mus
        threshold = math.floor(line)
        if line == threshold and line == int(line):
            threshold -= 1  # exact-integer line: under excludes the line itself
        for x, y in ((0, 0), (1, 0), (0, 1), (1, 1)):
            if x + y <= threshold:
                tau = _tau(x, y, mu_h, mu_a, state.rho)
                cell = _poisson_pmf(x, mu_h) * _poisson_pmf(y, mu_a)
                p_under += (tau - 1.0) * cell
        p_under = min(max(p_under, 1e-6), 1 - 1e-6)
    return (1.0 - p_under) if side == "over" else p_under


def total_pmf(state: DCState, match: dict[str, Any], k: int) -> float:
    return _poisson_pmf(k, predict_total_mu(state, match))


# ---------------------------------------------------------------------------
# Pipeline integration: cached fits + poisson_total-compatible prediction API
# ---------------------------------------------------------------------------

_DC_CACHE: dict[tuple, DCState | None] = {}


def _train_fingerprint(matches: list[dict[str, Any]]) -> tuple:
    if not matches:
        return (0,)
    first, last = matches[0], matches[-1]
    return (
        len(matches),
        first.get("match_id"), first.get("date"),
        last.get("match_id"), last.get("date"),
    )


def fit_dc_cached(
    matches: list[dict[str, Any]],
    *,
    stat: str,
    period: str,
    ref_date: date | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> DCState | None:
    """fit_dc with memoization — score_line is called per opportunity, and an
    L-BFGS refit per call would be far too slow."""
    key = (stat, period, ref_date, half_life_days, _train_fingerprint(matches))
    if key not in _DC_CACHE:
        _DC_CACHE[key] = fit_dc(
            matches, stat=stat, period=period, ref_date=ref_date, half_life_days=half_life_days
        )
    return _DC_CACHE[key]


def match_ref_date(match: dict[str, Any]) -> date | None:
    try:
        return parse_match_date(match["date"])
    except Exception:
        return None


def predict_side_probability_dc(
    state: DCState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    team_role: str | None = None,
) -> float:
    """Drop-in replacement for poisson_total.predict_side_probability.

    Simple lines use the DC pmf (with tau for match-total goals); quarter
    lines delegate to the settlement helper on the DC mu.
    """
    from pipeline.betting.settlement import parse_line, quarter_line_win_probability

    parsed = parse_line(line_raw)
    if parsed is None:
        return 0.0
    if team_role is not None:
        mus = predict_mus(state, match)
        if mus is None:
            return 0.0
        mu = mus[0] if team_role == "home" else mus[1]
        if len(parsed.parts) == 2:
            return quarter_line_win_probability(mu, line_raw, side)
        line = parsed.parts[0]
        return prob_over(mu, line) if side == "over" else prob_under(mu, line)
    if len(parsed.parts) == 2:
        mu = predict_total_mu(state, match)
        return quarter_line_win_probability(mu, line_raw, side)
    return predict_total_probability(state, match, line=parsed.parts[0], side=side)
