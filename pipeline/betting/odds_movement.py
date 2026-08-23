"""Build and score odds-movement signals from saved snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.betting.devig import devig_multi_way, implied_over_under
from pipeline.betting.markets import get_adapters
from pipeline.betting.recommend import load_target_matches
from pipeline.betting.types import BetOpportunity

from pipeline.betting.markets import DEFAULT_MARKET_KEYS


@dataclass(frozen=True)
class MovementPoint:
    snapshot_path: Path
    snapshot_at: datetime
    match: dict[str, Any]
    opportunity: BetOpportunity
    implied_prob: float | None
    is_favorite: bool


@dataclass(frozen=True)
class MovementSignal:
    snapshot_at: datetime
    match_id: str
    teams: str
    market: str
    line: str
    side: str
    current_odds: float
    implied_prob: float | None
    minutes_to_kickoff: float | None
    prev_odds: float | None
    odds_delta: float | None
    odds_delta_per_hour: float | None
    down_streak: int
    up_streak: int
    shorten_score: float
    reason: str

    def to_row(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "match_id": self.match_id,
            "teams": self.teams,
            "market": self.market,
            "line": self.line,
            "side": self.side,
            "current_odds": round(self.current_odds, 4),
            "implied_prob": round(self.implied_prob, 4) if self.implied_prob is not None else "",
            "minutes_to_kickoff": round(self.minutes_to_kickoff, 1) if self.minutes_to_kickoff is not None else "",
            "prev_odds": round(self.prev_odds, 4) if self.prev_odds is not None else "",
            "odds_delta": round(self.odds_delta, 4) if self.odds_delta is not None else "",
            "odds_delta_per_hour": round(self.odds_delta_per_hour, 4) if self.odds_delta_per_hour is not None else "",
            "down_streak": self.down_streak,
            "up_streak": self.up_streak,
            "shorten_score": round(self.shorten_score, 4),
            "reason": self.reason,
        }


def _parse_snapshot_at(payload: dict[str, Any], path: Path) -> datetime:
    raw = payload.get("snapshot_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _parse_kickoff(match: dict[str, Any]) -> datetime | None:
    raw = match.get("kick_off")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _key(opportunity: BetOpportunity) -> tuple[str, str, str, str]:
    return (opportunity.match_id, opportunity.market, opportunity.line, opportunity.side)


def _implied_map(opportunities: list[BetOpportunity]) -> dict[tuple[str, str, str, str], float]:
    out: dict[tuple[str, str, str, str], float] = {}
    grouped: dict[tuple[str, str], list[BetOpportunity]] = {}
    for opp in opportunities:
        grouped.setdefault((opp.market, opp.line), []).append(opp)

    for group in grouped.values():
        if len(group) == 2 and {opp.side for opp in group} == {"over", "under"}:
            for opp in group:
                implied = implied_over_under(opp.over_odds, opp.under_odds, opp.side)
                if implied is not None:
                    out[_key(opp)] = implied
            continue
        odds_by_side = {opp.side: opp.decimal_odds for opp in group}
        probs = devig_multi_way(odds_by_side) if len(odds_by_side) >= 2 else None
        if probs is None:
            continue
        for opp in group:
            if opp.side in probs:
                out[_key(opp)] = probs[opp.side]
    return out


def _load_points(path: Path, *, market_keys: list[str]) -> tuple[datetime, list[MovementPoint]]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot_at = _parse_snapshot_at(payload, path)
    adapters = get_adapters(market_keys)
    points: list[MovementPoint] = []
    for match in load_target_matches(path):
        match_opps: list[BetOpportunity] = []
        for adapter in adapters:
            match_opps.extend(adapter.extract_opportunities(match))
        implied = _implied_map(match_opps)
        max_prob_by_group: dict[tuple[str, str], float] = {}
        for opp in match_opps:
            p = implied.get(_key(opp))
            if p is not None:
                max_prob_by_group[(opp.market, opp.line)] = max(max_prob_by_group.get((opp.market, opp.line), 0.0), p)
        for opp in match_opps:
            p = implied.get(_key(opp))
            points.append(
                MovementPoint(
                    snapshot_path=path,
                    snapshot_at=snapshot_at,
                    match=match,
                    opportunity=opp,
                    implied_prob=p,
                    is_favorite=(p is not None and p == max_prob_by_group.get((opp.market, opp.line))),
                )
            )
    return snapshot_at, points


def load_snapshot_points(paths: list[Path], *, market_keys: list[str] | None = None) -> list[MovementPoint]:
    markets = market_keys or DEFAULT_MARKET_KEYS
    points: list[MovementPoint] = []
    for path in paths:
        _, loaded = _load_points(path, market_keys=markets)
        points.extend(loaded)
    return sorted(points, key=lambda p: (p.snapshot_at, p.opportunity.match_id, p.opportunity.market, p.opportunity.line, p.opportunity.side))


def _series(points: list[MovementPoint]) -> dict[tuple[str, str, str, str], list[MovementPoint]]:
    out: dict[tuple[str, str, str, str], list[MovementPoint]] = {}
    for point in points:
        out.setdefault(_key(point.opportunity), []).append(point)
    for values in out.values():
        values.sort(key=lambda p: p.snapshot_at)
    return out


def _streak(values: list[MovementPoint], idx: int, *, direction: str) -> int:
    count = 0
    for i in range(idx, 0, -1):
        now = values[i].opportunity.decimal_odds
        prev = values[i - 1].opportunity.decimal_odds
        if direction == "down" and now < prev:
            count += 1
        elif direction == "up" and now > prev:
            count += 1
        else:
            break
    return count


def _movement_reason(delta: float | None, down_streak: int, up_streak: int, minutes_to_kickoff: float | None) -> str:
    parts: list[str] = []
    if delta is not None:
        parts.append("odds falling" if delta < 0 else ("odds rising" if delta > 0 else "odds flat"))
    if down_streak >= 2:
        parts.append(f"{down_streak} consecutive drops")
    if up_streak >= 2:
        parts.append(f"{up_streak} consecutive rises")
    if minutes_to_kickoff is not None and 0 <= minutes_to_kickoff <= 180:
        parts.append("close to kickoff")
    return "; ".join(parts) if parts else "insufficient movement history"


def _shorten_score(
    *,
    odds_delta_per_hour: float | None,
    down_streak: int,
    up_streak: int,
    minutes_to_kickoff: float | None,
    is_favorite: bool,
) -> float:
    score = 0.5
    if odds_delta_per_hour is not None:
        pct_speed = odds_delta_per_hour
        if pct_speed < 0:
            score += min(0.25, -pct_speed * 3.0)
        elif pct_speed > 0:
            score -= min(0.25, pct_speed * 3.0)
    score += min(0.18, down_streak * 0.06)
    score -= min(0.18, up_streak * 0.06)
    if minutes_to_kickoff is not None:
        if 0 <= minutes_to_kickoff <= 180:
            score += 0.05
        elif minutes_to_kickoff > 1440:
            score -= 0.03
    if is_favorite:
        score += 0.02
    return max(0.0, min(1.0, score))


def current_movement_signals(points: list[MovementPoint]) -> list[MovementSignal]:
    rows: list[MovementSignal] = []
    for values in _series(points).values():
        if not values:
            continue
        idx = len(values) - 1
        point = values[idx]
        opp = point.opportunity
        prev_odds = values[idx - 1].opportunity.decimal_odds if idx > 0 else None
        delta = (opp.decimal_odds - prev_odds) if prev_odds is not None else None
        hours = None
        if idx > 0:
            elapsed = (point.snapshot_at - values[idx - 1].snapshot_at).total_seconds() / 3600.0
            if elapsed > 0 and prev_odds and prev_odds > 0:
                hours = ((opp.decimal_odds / prev_odds) - 1.0) / elapsed
        kickoff = _parse_kickoff(point.match)
        minutes_to_kickoff = None
        if kickoff is not None:
            minutes_to_kickoff = (kickoff - point.snapshot_at).total_seconds() / 60.0
        down_streak = _streak(values, idx, direction="down")
        up_streak = _streak(values, idx, direction="up")
        score = _shorten_score(
            odds_delta_per_hour=hours,
            down_streak=down_streak,
            up_streak=up_streak,
            minutes_to_kickoff=minutes_to_kickoff,
            is_favorite=point.is_favorite,
        )
        rows.append(
            MovementSignal(
                snapshot_at=point.snapshot_at,
                match_id=opp.match_id,
                teams=opp.teams,
                market=opp.market,
                line=opp.line,
                side=opp.side,
                current_odds=opp.decimal_odds,
                implied_prob=point.implied_prob,
                minutes_to_kickoff=minutes_to_kickoff,
                prev_odds=prev_odds,
                odds_delta=delta,
                odds_delta_per_hour=hours,
                down_streak=down_streak,
                up_streak=up_streak,
                shorten_score=score,
                reason=_movement_reason(delta, down_streak, up_streak, minutes_to_kickoff),
            )
        )
    return sorted(rows, key=lambda r: (-r.shorten_score, r.match_id, r.market, r.line, r.side))


def movement_training_rows(points: list[MovementPoint], *, horizon_minutes: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    horizon = timedelta(minutes=horizon_minutes)
    for values in _series(points).values():
        for idx, point in enumerate(values[:-1]):
            target_time = point.snapshot_at + horizon
            future = next((p for p in values[idx + 1 :] if p.snapshot_at >= target_time), None)
            if future is None:
                continue
            opp = point.opportunity
            prev_odds = values[idx - 1].opportunity.decimal_odds if idx > 0 else None
            delta = (opp.decimal_odds - prev_odds) if prev_odds is not None else None
            kickoff = _parse_kickoff(point.match)
            minutes_to_kickoff = (kickoff - point.snapshot_at).total_seconds() / 60.0 if kickoff is not None else None
            future_odds = future.opportunity.decimal_odds
            clv = opp.decimal_odds / future_odds - 1.0 if future_odds > 0 else math.nan
            rows.append(
                {
                    "snapshot_at": point.snapshot_at.isoformat(),
                    "future_at": future.snapshot_at.isoformat(),
                    "horizon_minutes": horizon_minutes,
                    "match_id": opp.match_id,
                    "teams": opp.teams,
                    "market": opp.market,
                    "line": opp.line,
                    "side": opp.side,
                    "current_odds": round(opp.decimal_odds, 4),
                    "future_odds": round(future_odds, 4),
                    "odds_delta": round(delta, 4) if delta is not None else "",
                    "implied_prob": round(point.implied_prob, 4) if point.implied_prob is not None else "",
                    "minutes_to_kickoff": round(minutes_to_kickoff, 1) if minutes_to_kickoff is not None else "",
                    "down_streak": _streak(values, idx, direction="down"),
                    "up_streak": _streak(values, idx, direction="up"),
                    "shortened": int(future_odds < opp.decimal_odds),
                    "clv_if_bet": round(clv, 6),
                }
            )
    return rows
