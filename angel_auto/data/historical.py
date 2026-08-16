"""Historical candle fetch via Angel One's REST API - used for the India VIX bootstrap
(IV Rank's 90-day rolling window) and, later, backtesting (Phase 8).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tenacity import retry, stop_after_attempt, wait_exponential

from angel_auto.broker.angelone_auth import AngelSession
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal

log = get_logger(__name__)


class HistoricalDataError(RuntimeError):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def fetch_daily_candles(
    session: AngelSession, exchange: str, symbol_token: str, from_date: date, to_date: date
) -> list[dict]:
    """Returns [{"date", "open", "high", "low", "close", "volume"}, ...], oldest first."""
    params = {
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": "ONE_DAY",
        "fromdate": from_date.strftime("%Y-%m-%d 09:15"),
        "todate": to_date.strftime("%Y-%m-%d 15:30"),
    }
    response = session.smart_connect.getCandleData(params)
    if not response or not response.get("status"):
        raise HistoricalDataError(f"historical data fetch failed: {(response or {}).get('message')}")

    candles = []
    for row in response.get("data") or []:
        ts_str, o, h, l, c, v = row
        candles.append(
            {"date": datetime.fromisoformat(ts_str).date(), "open": o, "high": h, "low": l, "close": c, "volume": v}
        )
    return candles


def bootstrap_vix_history(session: AngelSession, vix_token: str, lookback_days: int = 90) -> int:
    """Fetches the trailing `lookback_days` of India VIX daily closes and upserts them into
    IVHistory (upsert, not insert - safe to call repeatedly, e.g. once per app start, to
    backfill any day that was missed rather than assuming the window is already complete)."""
    to_date = date.today()
    from_date = to_date - timedelta(days=int(lookback_days * 1.6))  # padded for weekends/holidays
    candles = fetch_daily_candles(session, "NSE", vix_token, from_date, to_date)
    for candle in candles:
        journal.upsert_vix_close(candle["date"], candle["close"])
    log.info("vix_history_bootstrapped", candle_count=len(candles), from_date=str(from_date), to_date=str(to_date))
    return len(candles)


def capture_eod_vix_close(latest_vix: float, as_of: date | None = None) -> None:
    """End-of-day job: record today's live VIX reading as today's close, keeping the
    rolling window current without waiting for Angel One's own EOD candle to post."""
    if latest_vix <= 0:
        log.warning("eod_vix_capture_skipped_no_live_value")
        return
    journal.upsert_vix_close(as_of or date.today(), latest_vix)
    log.info("eod_vix_captured", vix=latest_vix)
