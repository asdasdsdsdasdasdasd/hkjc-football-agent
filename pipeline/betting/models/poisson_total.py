"""Poisson total-count model with competition context and team contributions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pipeline.betting.load import split_teams

TeamCompKey = tuple[str, str]  # (competition, team)

# World Cup / major tournament names use a lower team sample floor.
WC_COMPETITIONS = frozenset({"世盃", "世界杯", "World Cup"})

# BSD pre-match xG is full-match; scale for HT goal markets (~45% of FT goals).
HT_GOAL_XG_SHARE = 0.45
# When BSD xG is present, shrink Poisson μ toward it (Poisson over-shoots on sparse WC samples).
XG_BLEND_WEIGHT = 0.65
XG_UPWARD_CAP = 1.20


def min_team_matches_for_comp(comp: str, default: int = 3) -> int:
    return 2 if comp in WC_COMPETITIONS else default


def _poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(mu) - mu - math.lgamma(k + 1))


def poisson_cdf(k: int, mu: float) -> float:
    """P(X <= k) for Poisson(mu)."""
    if k < 0:
        return 0.0
    total = 0.0
    for i in range(k + 1):
        total += _poisson_pmf(i, mu)
    return min(max(total, 0.0), 1.0)


def prob_over(mu: float, line: float) -> float:
    threshold = math.floor(line)
    return 1.0 - poisson_cdf(threshold, mu)


def prob_under(mu: float, line: float) -> float:
    threshold = math.floor(line)
    if line == threshold and line == int(line):
        return poisson_cdf(threshold - 1, mu) if threshold > 0 else 0.0
    return poisson_cdf(threshold, mu)


def _competition(match: dict[str, Any]) -> str:
    return (match.get("competition") or "").strip() or "未知"


def _shrink(value: float, prior: float, n: int, min_samples: int, strength: float) -> float:
    """Blend estimate toward prior; more shrinkage when n is small."""
    if n <= 0:
        return prior
    weight = strength / (strength + n)
    if n < min_samples:
        weight = max(weight, 1.0 - n / min_samples)
    return weight * prior + (1.0 - weight) * value


@dataclass
class PoissonTotalState:
    global_mean: float
    competition_mean: dict[str, float] = field(default_factory=dict)
    competition_counts: dict[str, int] = field(default_factory=dict)
    home_contrib: dict[TeamCompKey, float] = field(default_factory=dict)
    away_contrib: dict[TeamCompKey, float] = field(default_factory=dict)
    home_counts: dict[TeamCompKey, int] = field(default_factory=dict)
    away_counts: dict[TeamCompKey, int] = field(default_factory=dict)
    shrink: float = 0.35
    min_team_matches: int = 3
    min_comp_matches: int = 15
    stat: str = "corners"
    period: str = "full_time"


def _corner_split(match: dict[str, Any], period: str) -> tuple[int, int, int] | None:
    block = (match.get("corners") or {}).get(period) or {}
    total = block.get("total")
    home = block.get("home")
    away = block.get("away")
    if total is None or home is None or away is None:
        return None
    return int(total), int(home), int(away)


def _goal_split(match: dict[str, Any], period: str) -> tuple[int, int, int] | None:
    scores = match.get("scores") or {}
    key = "full_time" if period == "full_time" else "half_time"
    score = scores.get(key)
    if not score:
        return None
    parts = score.replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        home, away = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return home + away, home, away


def collect_splits(
    matches: list[dict[str, Any]],
    *,
    stat: str,
    period: str,
) -> list[tuple[dict[str, Any], int, int, int]]:
    rows: list[tuple[dict[str, Any], int, int, int]] = []
    for m in matches:
        if stat == "corners":
            split = _corner_split(m, period)
        else:
            split = _goal_split(m, period)
        if split is not None:
            rows.append((m, *split))
    return rows


def fit_poisson_total(
    matches: list[dict[str, Any]],
    *,
    stat: str = "corners",
    period: str = "full_time",
    shrink: float = 0.35,
    min_team_matches: int = 3,
    min_comp_matches: int = 15,
) -> PoissonTotalState | None:
    rows = collect_splits(matches, stat=stat, period=period)
    if not rows:
        return None

    global_mean = sum(total for _, total, _, _ in rows) / len(rows)

    comp_totals: dict[str, list[int]] = {}
    home_vals: dict[TeamCompKey, list[int]] = {}
    away_vals: dict[TeamCompKey, list[int]] = {}

    for match, total, home_c, away_c in rows:
        comp = _competition(match)
        comp_totals.setdefault(comp, []).append(total)

        pair = split_teams(match.get("teams") or "")
        if pair is None:
            continue
        home_team, away_team = pair
        hk: TeamCompKey = (comp, home_team)
        ak: TeamCompKey = (comp, away_team)
        home_vals.setdefault(hk, []).append(home_c)
        away_vals.setdefault(ak, []).append(away_c)

    competition_mean = {c: sum(v) / len(v) for c, v in comp_totals.items()}
    competition_counts = {c: len(v) for c, v in comp_totals.items()}

    home_contrib: dict[TeamCompKey, float] = {}
    home_counts: dict[TeamCompKey, int] = {}
    for key, vals in home_vals.items():
        threshold = min_team_matches_for_comp(key[0], min_team_matches)
        if len(vals) >= threshold:
            home_contrib[key] = sum(vals) / len(vals)
            home_counts[key] = len(vals)

    away_contrib: dict[TeamCompKey, float] = {}
    away_counts: dict[TeamCompKey, int] = {}
    for key, vals in away_vals.items():
        threshold = min_team_matches_for_comp(key[0], min_team_matches)
        if len(vals) >= threshold:
            away_contrib[key] = sum(vals) / len(vals)
            away_counts[key] = len(vals)

    return PoissonTotalState(
        global_mean=global_mean,
        competition_mean=competition_mean,
        competition_counts=competition_counts,
        home_contrib=home_contrib,
        away_contrib=away_contrib,
        home_counts=home_counts,
        away_counts=away_counts,
        shrink=shrink,
        min_team_matches=min_team_matches,
        min_comp_matches=min_comp_matches,
        stat=stat,
        period=period,
    )


def _match_external_features(match: dict[str, Any]) -> dict[str, Any]:
    feat = match.get("external_features")
    return feat if isinstance(feat, dict) else {}


def _bsd_goal_mu(match: dict[str, Any], period: str) -> float | None:
    feat = _match_external_features(match)
    hx = feat.get("home_xg")
    ax = feat.get("away_xg")
    if hx is None or ax is None:
        return None
    total = float(hx) + float(ax)
    if period == "half_time":
        total *= HT_GOAL_XG_SHARE
    return total


def _blend_goal_mu_with_xg(mu_poisson: float, match: dict[str, Any], period: str) -> float:
    xg_mu = _bsd_goal_mu(match, period)
    if xg_mu is None:
        return mu_poisson
    blended = (1.0 - XG_BLEND_WEIGHT) * mu_poisson + XG_BLEND_WEIGHT * xg_mu
    capped = min(blended, xg_mu * XG_UPWARD_CAP)
    return max(capped, 0.01)


def _blend_team_goal_mu_with_xg(mu_poisson: float, match: dict[str, Any], period: str, team_role: str) -> float:
    feat = _match_external_features(match)
    raw = feat.get("home_xg") if team_role == "home" else feat.get("away_xg")
    if raw is None:
        return mu_poisson
    xg_mu = float(raw)
    if period == "half_time":
        xg_mu *= HT_GOAL_XG_SHARE
    blended = (1.0 - XG_BLEND_WEIGHT) * mu_poisson + XG_BLEND_WEIGHT * xg_mu
    capped = min(blended, xg_mu * XG_UPWARD_CAP)
    return max(capped, 0.01)


def _competition_baseline(state: PoissonTotalState, comp: str) -> float:
    n = state.competition_counts.get(comp, 0)
    raw = state.competition_mean.get(comp, state.global_mean)
    return _shrink(raw, state.global_mean, n, state.min_comp_matches, state.shrink * 10)


def _cross_comp_team_prior(
    state: PoissonTotalState,
    team: str,
    *,
    role: str,
    target_comp: str,
) -> float | None:
    """Weighted mean of the same team in other competitions (club / 國際賽 etc.)."""
    contrib = state.home_contrib if role == "home" else state.away_contrib
    counts = state.home_counts if role == "home" else state.away_counts
    weighted_sum = 0.0
    total_n = 0
    for (comp, name), rate in contrib.items():
        if name != team or comp == target_comp:
            continue
        n = counts.get((comp, name), 0)
        if n <= 0:
            continue
        weighted_sum += rate * n
        total_n += n
    if total_n <= 0:
        return None
    return weighted_sum / total_n


def _team_rate(
    state: PoissonTotalState,
    *,
    team: str,
    comp: str,
    role: str,
    comp_mu: float,
) -> float:
    half_prior = comp_mu / 2.0
    min_samples = min_team_matches_for_comp(comp, state.min_team_matches)
    key: TeamCompKey = (comp, team)
    contrib = state.home_contrib if role == "home" else state.away_contrib
    counts = state.home_counts if role == "home" else state.away_counts

    cross_prior = _cross_comp_team_prior(state, team, role=role, target_comp=comp)
    prior = cross_prior if cross_prior is not None else half_prior

    n = counts.get(key, 0)
    if n <= 0:
        return prior

    raw = contrib.get(key, prior)
    return _shrink(raw, prior, n, min_samples, state.shrink * 5)


def predict_match_mu(state: PoissonTotalState, match: dict[str, Any]) -> float:
    pair = split_teams(match.get("teams") or "")
    comp = _competition(match)
    comp_mu = _competition_baseline(state, comp)

    if pair is None:
        return max(comp_mu, 0.01)

    home_team, away_team = pair
    home_rate = _team_rate(state, team=home_team, comp=comp, role="home", comp_mu=comp_mu)
    away_rate = _team_rate(state, team=away_team, comp=comp, role="away", comp_mu=comp_mu)
    mu = max(home_rate + away_rate, 0.01)
    if state.stat == "goals":
        mu = _blend_goal_mu_with_xg(mu, match, state.period)
    return mu


def predict_team_mu(state: PoissonTotalState, match: dict[str, Any], *, team_role: str) -> float:
    """Expected count for one team (home or away) in the fitted stat/period."""
    pair = split_teams(match.get("teams") or "")
    comp = _competition(match)
    comp_mu = _competition_baseline(state, comp)

    if pair is None:
        mu = max(comp_mu / 2.0, 0.01)
    else:
        home_team, away_team = pair
        team = home_team if team_role == "home" else away_team
        role = "home" if team_role == "home" else "away"
        mu = max(_team_rate(state, team=team, comp=comp, role=role, comp_mu=comp_mu), 0.01)

    if state.stat == "goals":
        mu = _blend_team_goal_mu_with_xg(mu, match, state.period, team_role)
    return mu


def _resolve_mu(state: PoissonTotalState, match: dict[str, Any], *, team_role: str | None) -> float:
    if team_role is None:
        return predict_match_mu(state, match)
    return predict_team_mu(state, match, team_role=team_role)


def predict_side_probability(
    state: PoissonTotalState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    team_role: str | None = None,
) -> float:
    from pipeline.betting.settlement import parse_line, quarter_line_win_probability

    mu = _resolve_mu(state, match, team_role=team_role)
    parsed = parse_line(line_raw)
    if parsed is None:
        return 0.0
    if len(parsed.parts) == 2:
        return quarter_line_win_probability(mu, line_raw, side)
    line = parsed.parts[0]
    if side == "over":
        return prob_over(mu, line)
    if side == "under":
        return prob_under(mu, line)
    return 0.0


def expected_over_under_roi(
    state: PoissonTotalState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    decimal_odds: float,
    team_role: str | None = None,
) -> float:
    """Expected unit-stake ROI, including pushes and quarter-line half stakes."""
    from pipeline.betting.settlement import pnl_over_under

    mu = _resolve_mu(state, match, team_role=team_role)
    # Football goals/corners are low-count distributions; this keeps tail error tiny
    # without dragging scipy into the project.
    max_k = max(40, int(math.ceil(mu + 12.0 * math.sqrt(max(mu, 1.0)) + 20.0)))
    expected = 0.0
    mass = 0.0
    for k in range(max_k + 1):
        p = _poisson_pmf(k, mu)
        mass += p
        _, pnl = pnl_over_under(k, line_raw, side, decimal_odds, 1.0)
        expected += p * pnl

    if mass < 1.0:
        # The omitted tail is always on the over side of practical HKJC total lines.
        tail_total = max_k + 1
        _, tail_pnl = pnl_over_under(tail_total, line_raw, side, decimal_odds, 1.0)
        expected += max(0.0, 1.0 - mass) * tail_pnl
    return expected


def fair_over_under_odds(
    state: PoissonTotalState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    team_role: str | None = None,
) -> float | None:
    """Break-even decimal odds, including pushes and quarter-line half stakes."""
    from pipeline.betting.settlement import pnl_over_under

    mu = _resolve_mu(state, match, team_role=team_role)
    max_k = max(40, int(math.ceil(mu + 12.0 * math.sqrt(max(mu, 1.0)) + 20.0)))
    win_stake = 0.0
    lose_stake = 0.0
    mass = 0.0

    for k in range(max_k + 1):
        p = _poisson_pmf(k, mu)
        mass += p
        _, pnl_at_two = pnl_over_under(k, line_raw, side, 2.0, 1.0)
        if pnl_at_two > 0:
            win_stake += p * pnl_at_two
        elif pnl_at_two < 0:
            lose_stake += p * (-pnl_at_two)

    if mass < 1.0:
        tail_total = max_k + 1
        _, tail_pnl_at_two = pnl_over_under(tail_total, line_raw, side, 2.0, 1.0)
        tail = max(0.0, 1.0 - mass)
        if tail_pnl_at_two > 0:
            win_stake += tail * tail_pnl_at_two
        elif tail_pnl_at_two < 0:
            lose_stake += tail * (-tail_pnl_at_two)

    if win_stake <= 0:
        return None
    return 1.0 + (lose_stake / win_stake)
