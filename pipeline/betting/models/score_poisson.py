"""Scoreline Poisson model for match-result probabilities."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pipeline.betting.load import parse_match_date, split_teams
from pipeline.betting.models.external_features import feature_edge
from pipeline.betting.models.poisson_total import _competition, _shrink

TeamCompKey = tuple[str, str]


@dataclass(frozen=True)
class ScorelineState:
    global_home_mu: float
    global_away_mu: float
    comp_home_mu: dict[str, float] = field(default_factory=dict)
    comp_away_mu: dict[str, float] = field(default_factory=dict)
    comp_counts: dict[str, int] = field(default_factory=dict)
    home_attack: dict[TeamCompKey, float] = field(default_factory=dict)
    home_defense: dict[TeamCompKey, float] = field(default_factory=dict)
    away_attack: dict[TeamCompKey, float] = field(default_factory=dict)
    away_defense: dict[TeamCompKey, float] = field(default_factory=dict)
    home_counts: dict[TeamCompKey, int] = field(default_factory=dict)
    away_counts: dict[TeamCompKey, int] = field(default_factory=dict)
    recent_goal_diff: dict[str, float] = field(default_factory=dict)
    last_played: dict[str, date] = field(default_factory=dict)
    draw_multiplier: float = 1.0
    shrink: float = 0.55
    min_team_matches: int = 4
    min_comp_matches: int = 20
    max_goals: int = 10


def _parse_score_pair(score: str | None) -> tuple[int, int] | None:
    if not score:
        return None
    parts = score.replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        home, away = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return home, away


def _poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(mu) - mu - math.lgamma(k + 1))


def _safe_ratio(value: float, baseline: float) -> float:
    if baseline <= 0:
        return 1.0
    return max(0.05, value / baseline)


def _mean(values: list[float], fallback: float) -> float:
    return sum(values) / len(values) if values else fallback


def fit_scoreline_model(
    matches: list[dict[str, Any]],
    *,
    score_key: str = "full_time",
    shrink: float = 0.55,
    min_team_matches: int = 4,
    min_comp_matches: int = 20,
    max_goals: int = 10,
) -> ScorelineState | None:
    rows: list[tuple[dict[str, Any], int, int, str, str]] = []
    for match in matches:
        pair = split_teams(match.get("teams") or "")
        score = _parse_score_pair((match.get("scores") or {}).get(score_key))
        if pair is None or score is None:
            continue
        rows.append((match, score[0], score[1], pair[0], pair[1]))

    if not rows:
        return None

    global_home_mu = max(0.05, sum(h for _, h, _, _, _ in rows) / len(rows))
    global_away_mu = max(0.05, sum(a for _, _, a, _, _ in rows) / len(rows))

    comp_home: dict[str, list[int]] = {}
    comp_away: dict[str, list[int]] = {}
    home_for: dict[TeamCompKey, list[int]] = {}
    home_against: dict[TeamCompKey, list[int]] = {}
    away_for: dict[TeamCompKey, list[int]] = {}
    away_against: dict[TeamCompKey, list[int]] = {}
    team_recent: dict[str, list[tuple[date, int]]] = {}
    last_played: dict[str, date] = {}

    for match, home_goals, away_goals, home_team, away_team in rows:
        comp = _competition(match)
        try:
            match_date = parse_match_date(match["date"])
        except (KeyError, TypeError, ValueError):
            match_date = None
        comp_home.setdefault(comp, []).append(home_goals)
        comp_away.setdefault(comp, []).append(away_goals)
        hk = (comp, home_team)
        ak = (comp, away_team)
        home_for.setdefault(hk, []).append(home_goals)
        home_against.setdefault(hk, []).append(away_goals)
        away_for.setdefault(ak, []).append(away_goals)
        away_against.setdefault(ak, []).append(home_goals)
        if match_date is not None:
            team_recent.setdefault(home_team, []).append((match_date, home_goals - away_goals))
            team_recent.setdefault(away_team, []).append((match_date, away_goals - home_goals))
            last_played[home_team] = max(last_played.get(home_team, match_date), match_date)
            last_played[away_team] = max(last_played.get(away_team, match_date), match_date)

    comp_home_mu = {comp: _mean(vals, global_home_mu) for comp, vals in comp_home.items()}
    comp_away_mu = {comp: _mean(vals, global_away_mu) for comp, vals in comp_away.items()}
    comp_counts = {comp: len(vals) for comp, vals in comp_home.items()}

    home_attack: dict[TeamCompKey, float] = {}
    home_defense: dict[TeamCompKey, float] = {}
    away_attack: dict[TeamCompKey, float] = {}
    away_defense: dict[TeamCompKey, float] = {}
    home_counts: dict[TeamCompKey, int] = {}
    away_counts: dict[TeamCompKey, int] = {}

    for key, vals in home_for.items():
        if len(vals) >= min_team_matches:
            comp = key[0]
            home_attack[key] = _safe_ratio(_mean(vals, comp_home_mu.get(comp, global_home_mu)), comp_home_mu.get(comp, global_home_mu))
            home_counts[key] = len(vals)
    for key, vals in home_against.items():
        if len(vals) >= min_team_matches:
            comp = key[0]
            home_defense[key] = _safe_ratio(_mean(vals, comp_away_mu.get(comp, global_away_mu)), comp_away_mu.get(comp, global_away_mu))
    for key, vals in away_for.items():
        if len(vals) >= min_team_matches:
            comp = key[0]
            away_attack[key] = _safe_ratio(_mean(vals, comp_away_mu.get(comp, global_away_mu)), comp_away_mu.get(comp, global_away_mu))
            away_counts[key] = len(vals)
    for key, vals in away_against.items():
        if len(vals) >= min_team_matches:
            comp = key[0]
            away_defense[key] = _safe_ratio(_mean(vals, comp_home_mu.get(comp, global_home_mu)), comp_home_mu.get(comp, global_home_mu))

    recent_goal_diff = {}
    for team, vals in team_recent.items():
        recent = [gd for _, gd in sorted(vals, key=lambda item: item[0])[-5:]]
        if recent:
            recent_goal_diff[team] = sum(recent) / len(recent)

    state = ScorelineState(
        global_home_mu=global_home_mu,
        global_away_mu=global_away_mu,
        comp_home_mu=comp_home_mu,
        comp_away_mu=comp_away_mu,
        comp_counts=comp_counts,
        home_attack=home_attack,
        home_defense=home_defense,
        away_attack=away_attack,
        away_defense=away_defense,
        home_counts=home_counts,
        away_counts=away_counts,
        recent_goal_diff=recent_goal_diff,
        last_played=last_played,
        shrink=shrink,
        min_team_matches=min_team_matches,
        min_comp_matches=min_comp_matches,
        max_goals=max_goals,
    )
    raw_draw = sum(1 for _, h, a, _, _ in rows if h == a) / len(rows)
    pred_draws = [predict_1x2_probs(state, match)["draw"] for match, _, _, _, _ in rows]
    pred_draw = _mean(pred_draws, raw_draw)
    draw_multiplier = min(1.35, max(0.75, raw_draw / pred_draw)) if pred_draw > 0 else 1.0
    return ScorelineState(**{**state.__dict__, "draw_multiplier": draw_multiplier})


def predict_goal_means(state: ScorelineState, match: dict[str, Any]) -> tuple[float, float]:
    comp = _competition(match)
    comp_n = state.comp_counts.get(comp, 0)
    comp_home = _shrink(
        state.comp_home_mu.get(comp, state.global_home_mu),
        state.global_home_mu,
        comp_n,
        state.min_comp_matches,
        state.shrink * 10,
    )
    comp_away = _shrink(
        state.comp_away_mu.get(comp, state.global_away_mu),
        state.global_away_mu,
        comp_n,
        state.min_comp_matches,
        state.shrink * 10,
    )

    pair = split_teams(match.get("teams") or "")
    if pair is None:
        return max(0.05, comp_home), max(0.05, comp_away)

    home_team, away_team = pair
    hk = (comp, home_team)
    ak = (comp, away_team)
    home_n = state.home_counts.get(hk, 0)
    away_n = state.away_counts.get(ak, 0)
    home_attack = _shrink(state.home_attack.get(hk, 1.0), 1.0, home_n, state.min_team_matches, state.shrink * 5)
    home_defense = _shrink(state.home_defense.get(hk, 1.0), 1.0, home_n, state.min_team_matches, state.shrink * 5)
    away_attack = _shrink(state.away_attack.get(ak, 1.0), 1.0, away_n, state.min_team_matches, state.shrink * 5)
    away_defense = _shrink(state.away_defense.get(ak, 1.0), 1.0, away_n, state.min_team_matches, state.shrink * 5)

    home_mu = comp_home * home_attack * away_defense
    away_mu = comp_away * away_attack * home_defense
    home_form = max(-2.0, min(2.0, state.recent_goal_diff.get(home_team, 0.0)))
    away_form = max(-2.0, min(2.0, state.recent_goal_diff.get(away_team, 0.0)))
    form_edge = home_form - away_form
    home_mu *= math.exp(0.04 * form_edge)
    away_mu *= math.exp(-0.04 * form_edge)

    try:
        match_date = parse_match_date(match["date"])
    except (KeyError, TypeError, ValueError):
        match_date = None
    if match_date is not None:
        home_rest = (match_date - state.last_played[home_team]).days if home_team in state.last_played else None
        away_rest = (match_date - state.last_played[away_team]).days if away_team in state.last_played else None
        if home_rest is not None and away_rest is not None:
            rest_edge = max(-5, min(5, home_rest - away_rest))
            home_mu *= math.exp(0.015 * rest_edge)
            away_mu *= math.exp(-0.015 * rest_edge)

    features = match.get("external_features") if isinstance(match.get("external_features"), dict) else {}
    if features:
        xg_edge = feature_edge(features, "home_xg", "away_xg", cap=2.0)
        shots_edge = feature_edge(features, "home_shots", "away_shots", cap=12.0)
        lineup_edge = feature_edge(features, "home_lineup_strength", "away_lineup_strength", cap=1.0)
        injury_edge = feature_edge(features, "home_injuries", "away_injuries", cap=8.0)
        fatigue_edge = feature_edge(features, "home_fatigue", "away_fatigue", cap=5.0)
        try:
            team_news_edge = float(features.get("team_news_edge", 0.0) or 0.0)
        except (TypeError, ValueError):
            team_news_edge = 0.0
        team_news_edge = max(-1.0, min(1.0, team_news_edge))

        # Small coefficients: external data nudges the model, it does not replace market reality.
        attack_edge = 0.08 * xg_edge + 0.01 * shots_edge + 0.15 * lineup_edge + 0.08 * team_news_edge
        penalty_edge = -0.035 * injury_edge - 0.025 * fatigue_edge
        home_mu *= math.exp(attack_edge + penalty_edge)
        away_mu *= math.exp(-attack_edge - penalty_edge)

        try:
            weather_goal_factor = float(features.get("weather_goal_factor", 1.0))
        except (TypeError, ValueError):
            weather_goal_factor = 1.0
        weather_goal_factor = max(0.65, min(1.25, weather_goal_factor))
        home_mu *= weather_goal_factor
        away_mu *= weather_goal_factor
    return max(0.05, min(home_mu, 6.0)), max(0.05, min(away_mu, 6.0))


def predict_1x2_probs(state: ScorelineState, match: dict[str, Any]) -> dict[str, float]:
    home_mu, away_mu = predict_goal_means(state, match)
    probs = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for h in range(state.max_goals + 1):
        p_h = _poisson_pmf(h, home_mu)
        for a in range(state.max_goals + 1):
            p = p_h * _poisson_pmf(a, away_mu)
            if h > a:
                probs["home"] += p
            elif h < a:
                probs["away"] += p
            else:
                probs["draw"] += p * state.draw_multiplier

    total = sum(probs.values())
    if total <= 0:
        return {"home": 1 / 3, "draw": 1 / 3, "away": 1 / 3}
    return {side: max(0.0, probs[side] / total) for side in ("home", "draw", "away")}


# BSD pre-match xG is full-match; scale for HT (~45% of FT goals).
HT_GOAL_XG_SHARE = 0.45
TOTAL_XG_BLEND_WEIGHT = 0.55
TOTAL_XG_UPWARD_CAP = 1.20


def _bsd_total_xg(match: dict[str, Any], *, score_key: str) -> float | None:
    features = match.get("external_features") if isinstance(match.get("external_features"), dict) else {}
    hx = features.get("home_xg")
    ax = features.get("away_xg")
    if hx is None or ax is None:
        return None
    total = float(hx) + float(ax)
    if score_key == "half_time":
        total *= HT_GOAL_XG_SHARE
    return max(0.05, total)


def predict_total_mu(state: ScorelineState, match: dict[str, Any], *, score_key: str = "full_time") -> float:
    """Opponent-aware expected match goals, optionally shrunk toward BSD xG."""
    home_mu, away_mu = predict_goal_means(state, match)
    mu = max(0.05, home_mu + away_mu)
    xg_mu = _bsd_total_xg(match, score_key=score_key)
    if xg_mu is None:
        return mu
    blended = (1.0 - TOTAL_XG_BLEND_WEIGHT) * mu + TOTAL_XG_BLEND_WEIGHT * xg_mu
    return max(0.05, min(blended, xg_mu * TOTAL_XG_UPWARD_CAP))


def predict_total_side_probability(
    state: ScorelineState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    score_key: str = "full_time",
) -> float:
    """P(side) for match-total O/U under independent Poisson home/away goals."""
    from pipeline.betting.models.poisson_total import prob_over, prob_under
    from pipeline.betting.settlement import parse_line, quarter_line_win_probability

    mu = predict_total_mu(state, match, score_key=score_key)
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


def expected_total_over_under_roi(
    state: ScorelineState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    decimal_odds: float,
    score_key: str = "full_time",
) -> float:
    """Unit-stake expected ROI for match totals, including pushes and quarter lines."""
    from pipeline.betting.settlement import pnl_over_under

    mu = predict_total_mu(state, match, score_key=score_key)
    max_k = max(40, int(math.ceil(mu + 12.0 * math.sqrt(max(mu, 1.0)) + 20.0)))
    expected = 0.0
    mass = 0.0
    for k in range(max_k + 1):
        p = _poisson_pmf(k, mu)
        mass += p
        _, pnl = pnl_over_under(k, line_raw, side, decimal_odds, 1.0)
        expected += p * pnl
    if mass < 1.0:
        _, tail_pnl = pnl_over_under(max_k + 1, line_raw, side, decimal_odds, 1.0)
        expected += max(0.0, 1.0 - mass) * tail_pnl
    return expected


def fair_total_over_under_odds(
    state: ScorelineState,
    match: dict[str, Any],
    *,
    line_raw: str,
    side: str,
    score_key: str = "full_time",
) -> float | None:
    from pipeline.betting.settlement import pnl_over_under

    mu = predict_total_mu(state, match, score_key=score_key)
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
        tail = max(0.0, 1.0 - mass)
        _, tail_pnl_at_two = pnl_over_under(max_k + 1, line_raw, side, 2.0, 1.0)
        if tail_pnl_at_two > 0:
            win_stake += tail * tail_pnl_at_two
        elif tail_pnl_at_two < 0:
            lose_stake += tail * (-tail_pnl_at_two)
    if win_stake <= 0:
        return None
    return 1.0 + (lose_stake / win_stake)
