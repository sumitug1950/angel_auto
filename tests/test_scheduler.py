from datetime import date, datetime, time

from angel_auto.core.enums import Direction, PendingRequestStatus
from angel_auto.persistence import journal
from angel_auto.scheduler.jobs import (
    SchedulerService,
    cancel_stale_pending_request,
    is_market_open,
    is_position_expiry_today,
    is_trading_day,
    parse_hhmm,
)

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def test_parse_hhmm():
    assert parse_hhmm("15:15") == time(15, 15)
    assert parse_hhmm("09:05") == time(9, 5)


def test_is_trading_day_weekday():
    assert is_trading_day(date(2026, 8, 17)) is True  # Monday


def test_is_trading_day_weekend():
    assert is_trading_day(date(2026, 8, 16)) is False  # Sunday
    assert is_trading_day(date(2026, 8, 22)) is False  # Saturday


def test_is_trading_day_holiday():
    holidays = {date(2026, 8, 17)}
    assert is_trading_day(date(2026, 8, 17), holidays=holidays) is False


def test_is_market_open_within_hours():
    now = datetime(2026, 8, 17, 11, 0)
    assert is_market_open(now, MARKET_OPEN, MARKET_CLOSE) is True


def test_is_market_open_before_open():
    now = datetime(2026, 8, 17, 9, 0)
    assert is_market_open(now, MARKET_OPEN, MARKET_CLOSE) is False


def test_is_market_open_after_close():
    now = datetime(2026, 8, 17, 16, 0)
    assert is_market_open(now, MARKET_OPEN, MARKET_CLOSE) is False


def test_is_market_open_false_on_weekend_even_within_hours():
    now = datetime(2026, 8, 16, 11, 0)  # Sunday
    assert is_market_open(now, MARKET_OPEN, MARKET_CLOSE) is False


def test_is_position_expiry_today_true():
    position = {"expiry": "17AUG2026"}
    assert is_position_expiry_today(position, date(2026, 8, 17)) is True


def test_is_position_expiry_today_false():
    position = {"expiry": "29SEP2026"}
    assert is_position_expiry_today(position, date(2026, 8, 17)) is False


def test_is_position_expiry_today_none_position():
    assert is_position_expiry_today(None, date(2026, 8, 17)) is False


def test_cancel_stale_pending_request():
    journal.create_direction_request(Direction.LONG, macd_state_at_request="BULLISH")
    assert journal.get_pending_direction_request() is not None

    cancel_stale_pending_request()

    assert journal.get_pending_direction_request() is None


def test_cancel_stale_pending_request_noop_when_none_pending():
    cancel_stale_pending_request()  # should not raise
    assert journal.get_pending_direction_request() is None


# --- SchedulerService guarded handlers (direct calls, not real cron firing) -----------


def _make_service(is_expiry_day, square_off_calls: list):
    return SchedulerService(
        timezone="Asia/Kolkata",
        square_off_normal_time="15:15",
        square_off_expiry_day_time="15:00",
        daily_relogin_time="08:45",
        on_square_off=lambda: square_off_calls.append("fired"),
        on_daily_relogin=lambda: None,
        is_expiry_day=is_expiry_day,
    )


def test_normal_square_off_fires_when_not_expiry_day():
    calls = []
    service = _make_service(is_expiry_day=lambda: False, square_off_calls=calls)
    service._normal_square_off()
    assert calls == ["fired"]


def test_normal_square_off_skipped_on_expiry_day():
    calls = []
    service = _make_service(is_expiry_day=lambda: True, square_off_calls=calls)
    service._normal_square_off()
    assert calls == []  # expiry-day time is what should fire instead


def test_expiry_day_square_off_fires_only_on_expiry_day():
    calls = []
    service = _make_service(is_expiry_day=lambda: True, square_off_calls=calls)
    service._expiry_day_square_off()
    assert calls == ["fired"]


def test_expiry_day_square_off_skipped_on_normal_day():
    calls = []
    service = _make_service(is_expiry_day=lambda: False, square_off_calls=calls)
    service._expiry_day_square_off()
    assert calls == []


def test_scheduler_jobs_registered_at_configured_times():
    service = _make_service(is_expiry_day=lambda: False, square_off_calls=[])
    service.start()
    try:
        job_ids = {job.id for job in service.scheduler.get_jobs()}
        assert job_ids == {"square_off_normal", "square_off_expiry_day", "daily_reset", "daily_relogin"}

        normal_job = service.scheduler.get_job("square_off_normal")
        # CronTrigger fields expose hour/minute - confirm they match config (15:15)
        field_map = {f.name: str(f) for f in normal_job.trigger.fields}
        assert field_map["hour"] == "15"
        assert field_map["minute"] == "15"

        expiry_job = service.scheduler.get_job("square_off_expiry_day")
        field_map = {f.name: str(f) for f in expiry_job.trigger.fields}
        assert field_map["hour"] == "15"
        assert field_map["minute"] == "0"
    finally:
        service.shutdown()
