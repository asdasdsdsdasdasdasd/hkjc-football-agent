"""Settlement helpers for O/U and quarter lines."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.betting.types import BetOutcome

_LINE_RE = re.compile(r"^\[([^\]]+)\]$")


@dataclass(frozen=True)
class ParsedLine:
    raw: str
    parts: tuple[float, ...]


def parse_line(raw: str) -> ParsedLine | None:
    raw = raw.strip()
    m = _LINE_RE.match(raw)
    if not m:
        return None
    inner = m.group(1)
    nums: list[float] = []
    for chunk in inner.split("/"):
        chunk = chunk.strip()
        if not chunk:
            return None
        try:
            nums.append(float(chunk))
        except ValueError:
            return None
    if not nums:
        return None
    return ParsedLine(raw=raw, parts=tuple(nums))


def _ou_result(total: float, line: float, side: str) -> BetOutcome:
    if total == line:
        return BetOutcome.PUSH
    over_wins = total > line
    if side == "over":
        return BetOutcome.WIN if over_wins else BetOutcome.LOSE
    if side == "under":
        return BetOutcome.WIN if not over_wins else BetOutcome.LOSE
    return BetOutcome.UNKNOWN


def settle_over_under(total: int | None, line_raw: str, side: str) -> BetOutcome:
    """Settle over/under for standard and quarter lines."""
    if total is None:
        return BetOutcome.UNKNOWN
    parsed = parse_line(line_raw)
    if parsed is None:
        return BetOutcome.UNKNOWN

    parts = parsed.parts
    if len(parts) == 1:
        return _ou_result(float(total), parts[0], side)

    if len(parts) == 2:
        low, high = parts
        r_low = _ou_result(float(total), low, side)
        r_high = _ou_result(float(total), high, side)
        if r_low == r_high:
            return r_low
        # Quarter line: one half wins, one half pushes -> treat as WIN for hit-rate;
        # use pnl_over_under() for accurate P&L.
        if r_low == BetOutcome.WIN or r_high == BetOutcome.WIN:
            return BetOutcome.WIN
        if r_low == BetOutcome.PUSH and r_high == BetOutcome.PUSH:
            return BetOutcome.PUSH
        if BetOutcome.PUSH in (r_low, r_high) and BetOutcome.LOSE in (r_low, r_high):
            return BetOutcome.LOSE
        return BetOutcome.UNKNOWN

    return BetOutcome.UNKNOWN


def pnl_over_under(
    total: int | None,
    line_raw: str,
    side: str,
    decimal_odds: float,
    stake: float,
) -> tuple[BetOutcome, float]:
    """Return settlement outcome and accurate P&L (handles quarter lines)."""
    if total is None:
        return BetOutcome.UNKNOWN, 0.0
    parsed = parse_line(line_raw)
    if parsed is None:
        return BetOutcome.UNKNOWN, 0.0

    if len(parsed.parts) == 1:
        outcome = _ou_result(float(total), parsed.parts[0], side)
        return outcome, pnl_for_outcome(outcome, decimal_odds, stake)

    if len(parsed.parts) == 2:
        low, high = parsed.parts
        r_low = _ou_result(float(total), low, side)
        r_high = _ou_result(float(total), high, side)
        half = stake / 2.0
        pnl = pnl_for_outcome(r_low, decimal_odds, half) + pnl_for_outcome(r_high, decimal_odds, half)
        outcome = settle_over_under(total, line_raw, side)
        return outcome, pnl

    return BetOutcome.UNKNOWN, 0.0


def pnl_for_outcome(outcome: BetOutcome, decimal_odds: float, stake: float) -> float:
    if outcome == BetOutcome.WIN:
        return stake * (decimal_odds - 1.0)
    if outcome == BetOutcome.LOSE:
        return -stake
    if outcome == BetOutcome.PUSH:
        return 0.0
    return 0.0


def quarter_line_win_probability(total_mu: float, line_raw: str, side: str) -> float:
    """Expected win probability for quarter lines (half stake win, half push on split)."""
    parsed = parse_line(line_raw)
    if parsed is None:
        return 0.0
    if len(parsed.parts) == 1:
        from pipeline.betting.models.poisson_total import prob_over, prob_under

        line = parsed.parts[0]
        return prob_over(total_mu, line) if side == "over" else prob_under(total_mu, line)

    if len(parsed.parts) == 2:
        from pipeline.betting.models.poisson_total import prob_over, prob_under

        low, high = parsed.parts
        if side == "over":
            p_low = prob_over(total_mu, low)
            p_high = prob_over(total_mu, high)
        else:
            p_low = prob_under(total_mu, low)
            p_high = prob_under(total_mu, high)
        # Half on each sub-line; push on a sub-line counts as 0.5 win credit for EV.
        return 0.5 * p_low + 0.5 * p_high

    return 0.0
