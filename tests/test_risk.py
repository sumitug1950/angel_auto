from angel_auto.persistence import journal
from angel_auto.risk import circuit_breaker, pretrade


# --- pretrade checks ------------------------------------------------------


def test_pretrade_allows_when_clean():
    result = pretrade.run_pretrade_checks(max_trades_per_day=2)
    assert result.allowed is True


def test_pretrade_blocks_when_halted():
    journal.set_trading_halt("test halt")
    result = pretrade.run_pretrade_checks(max_trades_per_day=2)
    assert result.allowed is False
    assert "test halt" in result.reason


def test_pretrade_blocks_at_daily_trade_cap():
    journal.increment_daily_trade_count()
    journal.increment_daily_trade_count()
    result = pretrade.run_pretrade_checks(max_trades_per_day=2)
    assert result.allowed is False
    assert "daily trade cap" in result.reason


def test_pretrade_allows_below_daily_trade_cap():
    journal.increment_daily_trade_count()
    result = pretrade.run_pretrade_checks(max_trades_per_day=2)
    assert result.allowed is True


def test_pretrade_blocks_when_position_already_open():
    from angel_auto.core.enums import Direction, StructureType

    journal.create_position(Direction.LONG, StructureType.DEBIT, expiry="29SEP2026")
    result = pretrade.run_pretrade_checks(max_trades_per_day=2, max_concurrent_positions=1)
    assert result.allowed is False
    assert "already open" in result.reason


def test_pretrade_check_order_halted_wins_over_trade_cap():
    # both conditions true - halted should be reported (checked first), not the cap
    journal.set_trading_halt("halted first")
    journal.increment_daily_trade_count()
    journal.increment_daily_trade_count()
    result = pretrade.run_pretrade_checks(max_trades_per_day=2)
    assert result.allowed is False
    assert "halted first" in result.reason


# --- circuit breaker --------------------------------------------------------


def test_circuit_breaker_halts_on_daily_loss_limit():
    journal.record_trade_pnl(-4000)
    journal.record_trade_pnl(-4000)  # total -8000, at the configured limit

    circuit_breaker.evaluate_after_trade_close(daily_loss_limit_rs=8000, max_consecutive_losses=5)

    state = journal.get_or_create_daily_state()
    assert state["trading_halted"] is True
    assert "daily loss limit" in state["halt_reason"]


def test_circuit_breaker_does_not_halt_below_loss_limit():
    journal.record_trade_pnl(-3000)

    circuit_breaker.evaluate_after_trade_close(daily_loss_limit_rs=8000, max_consecutive_losses=5)

    state = journal.get_or_create_daily_state()
    assert state["trading_halted"] is False


def test_circuit_breaker_halts_on_consecutive_losses():
    journal.record_trade_pnl(-1000)
    journal.record_trade_pnl(-1000)

    circuit_breaker.evaluate_after_trade_close(daily_loss_limit_rs=50000, max_consecutive_losses=2)

    state = journal.get_or_create_daily_state()
    assert state["trading_halted"] is True
    assert "consecutive losses" in state["halt_reason"]


def test_circuit_breaker_resets_consecutive_losses_on_win():
    journal.record_trade_pnl(-1000)
    journal.record_trade_pnl(2000)  # win resets the streak

    circuit_breaker.evaluate_after_trade_close(daily_loss_limit_rs=50000, max_consecutive_losses=2)

    state = journal.get_or_create_daily_state()
    assert state["trading_halted"] is False
    assert state["consecutive_losses"] == 0


def test_circuit_breaker_is_idempotent_once_halted():
    journal.set_trading_halt("already halted for a different reason")
    journal.record_trade_pnl(-100000)  # would also trip the loss limit

    circuit_breaker.evaluate_after_trade_close(daily_loss_limit_rs=8000, max_consecutive_losses=5)

    state = journal.get_or_create_daily_state()
    assert state["halt_reason"] == "already halted for a different reason"  # not overwritten


def test_kill_switch_halts_trading():
    circuit_breaker.trigger_kill_switch("user pressed kill switch")
    state = journal.get_or_create_daily_state()
    assert state["trading_halted"] is True
    assert state["halt_reason"] == "user pressed kill switch"
