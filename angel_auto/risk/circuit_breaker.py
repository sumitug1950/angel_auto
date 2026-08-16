"""Daily loss / consecutive-loss circuit breaker, plus the kill-switch. Halting here means
setting DailyRiskState.trading_halted, which pretrade.check_trading_halted() then blocks
on - and because it's persisted (not an in-memory flag), a process restart doesn't silently
resume trading after a halt.

evaluate_after_trade_close() should be called once a closing fill is confirmed (the OMS's
job in Phase 6, right after it calls journal.close_position()) - it only cares about
realized P&L, so there's no reason to call it on every tick.
"""
from __future__ import annotations

from datetime import date

from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal

log = get_logger(__name__)


def evaluate_after_trade_close(
    daily_loss_limit_rs: float, max_consecutive_losses: int, trade_date: date | None = None
) -> None:
    """Idempotent - safe to call after every trade close. No-ops if already halted.

    `trade_date` defaults to real today; the backtest engine passes its simulated day so
    this scopes to the day being replayed, not the wall-clock date the backtest runs on."""
    state = journal.get_or_create_daily_state(trade_date)
    if state["trading_halted"]:
        return

    if state["realized_pnl_rs"] <= -abs(daily_loss_limit_rs):
        reason = f"daily loss limit hit: realized {state['realized_pnl_rs']:.2f} <= -{abs(daily_loss_limit_rs):.2f}"
        journal.set_trading_halt(reason, trade_date)
        log.warning("circuit_breaker_daily_loss_limit", realized_pnl_rs=state["realized_pnl_rs"])
        return

    if state["consecutive_losses"] >= max_consecutive_losses:
        reason = f"max consecutive losses hit: {state['consecutive_losses']} >= {max_consecutive_losses}"
        journal.set_trading_halt(reason, trade_date)
        log.warning("circuit_breaker_consecutive_losses", consecutive_losses=state["consecutive_losses"])


def trigger_kill_switch(reason: str = "manual kill-switch") -> None:
    """Dashboard kill-switch button. Halts new entries immediately; the caller is still
    responsible for also force-exiting any open position (strategy.manual_exit())."""
    journal.set_trading_halt(reason)
    log.warning("kill_switch_triggered", reason=reason)
