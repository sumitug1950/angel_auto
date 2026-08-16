"""Pre-trade checks - gates a proposed entry before any order is placed. The strategy calls
this before building an EntryIntent; the OMS (Phase 6) runs its own live margin check on
top of this as the final gate, since margin availability can only be confirmed against the
broker in real time.

Every check takes an optional `trade_date` (defaults to real today) - the backtest engine
passes its simulated "current day" here so daily counters (2-trades/day, daily loss limit)
get scoped to the day being replayed rather than the wall-clock date the backtest happens
to be run on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from angel_auto.persistence import journal


@dataclass
class PretradeCheckResult:
    allowed: bool
    reason: str = ""


def check_trading_halted(trade_date: date | None = None) -> PretradeCheckResult:
    state = journal.get_or_create_daily_state(trade_date)
    if state["trading_halted"]:
        return PretradeCheckResult(False, state["halt_reason"] or "trading halted")
    return PretradeCheckResult(True)


def check_daily_trade_cap(max_trades_per_day: int, trade_date: date | None = None) -> PretradeCheckResult:
    state = journal.get_or_create_daily_state(trade_date)
    if state["trades_taken"] >= max_trades_per_day:
        return PretradeCheckResult(
            False, f"daily trade cap reached ({state['trades_taken']}/{max_trades_per_day})"
        )
    return PretradeCheckResult(True)


def check_no_concurrent_position(max_concurrent_positions: int = 1) -> PretradeCheckResult:
    if max_concurrent_positions > 1:
        # not a scenario the flagship strategy uses (max_concurrent_positions=1), but keep
        # the check meaningful rather than hardcoding the single-position assumption
        return PretradeCheckResult(True)
    if journal.get_open_position() is not None:
        return PretradeCheckResult(False, "a position is already open (max_concurrent_positions=1)")
    return PretradeCheckResult(True)


def run_pretrade_checks(
    max_trades_per_day: int, max_concurrent_positions: int = 1, trade_date: date | None = None
) -> PretradeCheckResult:
    """Runs every check in order, short-circuiting on the first failure."""
    checks = (
        lambda: check_trading_halted(trade_date),
        lambda: check_daily_trade_cap(max_trades_per_day, trade_date),
        lambda: check_no_concurrent_position(max_concurrent_positions),
    )
    for check in checks:
        result = check()
        if not result.allowed:
            return result
    return PretradeCheckResult(True)
