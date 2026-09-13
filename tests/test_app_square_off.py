from datetime import datetime
from zoneinfo import ZoneInfo

from angel_auto.core.app import TradingApp
from angel_auto.core.enums import Direction, ExitReason, PositionStatus, StructureType
from angel_auto.persistence import journal
from angel_auto.strategy.base import ExitIntent

IST = ZoneInfo("Asia/Kolkata")


def test_square_off_is_due_from_the_square_off_time_on_trading_days():
    app = TradingApp()
    assert app._square_off_due(datetime(2026, 9, 14, 15, 14, tzinfo=IST)) is False  # Monday
    assert app._square_off_due(datetime(2026, 9, 14, 15, 15, tzinfo=IST)) is True
    assert app._square_off_due(datetime(2026, 9, 14, 15, 40, tzinfo=IST)) is True
    assert app._square_off_due(datetime(2026, 9, 12, 15, 20, tzinfo=IST)) is False  # Saturday


def test_square_off_uses_the_expiry_day_time_when_the_open_position_expires_today():
    app = TradingApp()
    position_id = journal.create_position(Direction.SHORT, StructureType.CREDIT, "14SEP2026")
    journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)

    assert app._square_off_due(datetime(2026, 9, 14, 15, 5, tzinfo=IST)) is True
    assert app._square_off_due(datetime(2026, 9, 14, 14, 59, tzinfo=IST)) is False


class _StubStrategy:
    def __init__(self):
        self.notices = []

    def on_square_off_trigger(self):
        return ExitIntent(reason=ExitReason.SQUARE_OFF)

    def cancel_pending_request(self):
        return True

    def record_entry_notice(self, message):
        self.notices.append(message)


class _RejectingOms:
    def __init__(self):
        self.exit_calls = 0
        self.last_notice = "Exit nahi hua"

    def execute_exit(self, intent, **kwargs):
        self.exit_calls += 1
        return False

    def backup_sl_triggered(self):
        raise AssertionError("no entries/exit checks after square-off time")


def test_failed_square_off_is_retried_every_cycle():
    app = TradingApp()
    app.strategy, app.oms = _StubStrategy(), _RejectingOms()
    app._square_off_due = lambda now=None: True
    position_id = journal.create_position(Direction.LONG, StructureType.DEBIT, "29SEP2026")
    journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)

    app._run_cycle()
    app._run_cycle()

    assert app.oms.exit_calls == 2
    assert app.strategy.notices == ["Exit nahi hua", "Exit nahi hua"]
