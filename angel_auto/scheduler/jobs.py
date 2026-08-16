"""Market-hours gating and scheduled jobs (mandatory square-off, daily reset, daily
re-login) via APScheduler. The mandatory square-off is a hard failsafe - it is not
strategy-toggleable, and it fires from here regardless of what the strategy's own exit
logic is doing.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from angel_auto.core.enums import PendingRequestStatus
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal

log = get_logger(__name__)


def parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def is_trading_day(d: date, holidays: set[date] | None = None) -> bool:
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if holidays and d in holidays:
        return False
    return True


def is_market_open(now: datetime, market_open: dtime, market_close: dtime, holidays: set[date] | None = None) -> bool:
    if not is_trading_day(now.date(), holidays):
        return False
    return market_open <= now.time() <= market_close


def is_position_expiry_today(open_position: dict | None, today: date) -> bool:
    """Is `today` the expiry date of the currently open position (if any)? Determines which
    of the two square-off times (normal vs. tighter expiry-day) applies."""
    if open_position is None:
        return False
    expiry_date = datetime.strptime(open_position["expiry"], "%d%b%Y").date()
    return expiry_date == today


def cancel_stale_pending_request() -> None:
    """Daily reset job: a Pending direction request left over from a previous day shouldn't
    silently execute today against a fresh MACD context - cancel it outright."""
    pending = journal.get_pending_direction_request()
    if pending is not None:
        journal.resolve_direction_request(pending["id"], PendingRequestStatus.CANCELLED)
        log.info("daily_reset_cancelled_stale_pending_request", request_id=pending["id"])


class SchedulerService:
    """Wraps APScheduler with the specific jobs this system needs. Callbacks are injected
    so this module never needs to import strategy/OMS/broker internals directly."""

    def __init__(
        self,
        timezone: str,
        square_off_normal_time: str,
        square_off_expiry_day_time: str,
        daily_relogin_time: str,
        on_square_off: Callable[[], None],
        on_daily_relogin: Callable[[], None],
        is_expiry_day: Callable[[], bool],
        on_daily_reset: Callable[[], None] = cancel_stale_pending_request,
    ) -> None:
        self.tz = ZoneInfo(timezone)
        self.scheduler = BackgroundScheduler(timezone=self.tz)
        self._square_off_normal = parse_hhmm(square_off_normal_time)
        self._square_off_expiry_day = parse_hhmm(square_off_expiry_day_time)
        self._relogin_time = parse_hhmm(daily_relogin_time)
        self.on_square_off = on_square_off
        self.on_daily_relogin = on_daily_relogin
        self.on_daily_reset = on_daily_reset
        self.is_expiry_day = is_expiry_day

    def start(self) -> None:
        self.scheduler.add_job(
            self._normal_square_off,
            CronTrigger(
                hour=self._square_off_normal.hour, minute=self._square_off_normal.minute,
                day_of_week="mon-fri", timezone=self.tz,
            ),
            id="square_off_normal",
        )
        self.scheduler.add_job(
            self._expiry_day_square_off,
            CronTrigger(
                hour=self._square_off_expiry_day.hour, minute=self._square_off_expiry_day.minute,
                day_of_week="mon-fri", timezone=self.tz,
            ),
            id="square_off_expiry_day",
        )
        self.scheduler.add_job(
            self.on_daily_reset,
            CronTrigger(hour=8, minute=30, day_of_week="mon-fri", timezone=self.tz),
            id="daily_reset",
        )
        self.scheduler.add_job(
            self.on_daily_relogin,
            CronTrigger(hour=self._relogin_time.hour, minute=self._relogin_time.minute, day_of_week="mon-fri", timezone=self.tz),
            id="daily_relogin",
        )
        self.scheduler.start()
        log.info("scheduler_started")

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def _normal_square_off(self) -> None:
        if not self.is_expiry_day():
            log.info("mandatory_square_off_triggered", kind="normal")
            self.on_square_off()

    def _expiry_day_square_off(self) -> None:
        if self.is_expiry_day():
            log.info("mandatory_square_off_triggered", kind="expiry_day")
            self.on_square_off()
