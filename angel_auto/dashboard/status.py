"""Payload builders shared by the REST routes and the WebSocket push, so /api/status and
/ws/status can never drift apart, and the chart's history (REST) and live updates (WS)
describe candles the same way."""
from __future__ import annotations

import math
from datetime import datetime

from angel_auto.analytics.indicators import compute_macd
from angel_auto.core.enums import OrderSide, StructureType
from angel_auto.data.market_data import Candle
from angel_auto.persistence import journal
from angel_auto.scheduler.jobs import is_market_open, parse_hhmm


def candle_point(candle: Candle) -> dict:
    return {
        "time": int(candle.start.timestamp()),
        "open": round(candle.open, 2),
        "high": round(candle.high, 2),
        "low": round(candle.low, 2),
        "close": round(candle.close, 2),
    }


def macd_points(trading_app) -> list[dict]:
    """The flagship's MACD (config periods) on every candle so far - the same numbers that
    gate its entries."""
    closes = trading_app.bars.closes_series()
    if len(closes) < 2:
        return []
    macd_cfg = trading_app.settings.strategies.active.macd
    macd_df = compute_macd(closes, macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)
    return [
        {
            "time": int(ts.timestamp()),
            "macd": round(float(row.macd), 4),
            "signal": round(float(row.signal), 4),
            "histogram": round(float(row.histogram), 4),
        }
        for ts, row in zip(macd_df.index, macd_df.itertuples(index=False))
    ]


def build_chart_history(trading_app) -> dict:
    return {
        "interval_sec": trading_app.settings.strategies.active.candle_interval_sec,
        "candles": [candle_point(c) for c in trading_app.bars.candles()],
        "macd": macd_points(trading_app),
    }


def _with_live_leg_prices(trading_app, position: dict) -> dict:
    legs = []
    for leg in position["legs"]:
        quote = trading_app.option_chain.get(leg["token"])
        ltp = quote.ltp if quote is not None and quote.ltp > 0 else None
        mark = leg.get("exit_price") if leg.get("exit_price") is not None else ltp  # an already-closed leg is locked in
        pnl = None
        if mark is not None and leg["entry_price"] is not None:
            per_unit = mark - leg["entry_price"] if leg["side"] == OrderSide.BUY else leg["entry_price"] - mark
            pnl = per_unit * leg["quantity"]
        legs.append({**leg, "ltp": ltp, "pnl_rs": pnl})
    return {**position, "legs": legs}


def build_status_payload(trading_app) -> dict:
    settings = trading_app.settings
    strategy = trading_app.strategy
    strat_cfg = settings.strategies.active
    risk = settings.app.risk

    open_position = journal.get_open_position() if strategy is not None else None
    market_open = is_market_open(
        datetime.now(trading_app._scheduler.tz),
        parse_hhmm(settings.app.market_hours.open),
        parse_hhmm(settings.app.market_hours.close),
    )
    router = trading_app._router
    feed_age = router.seconds_since_spot_tick() if hasattr(router, "seconds_since_spot_tick") else math.inf
    return {
        "mode": settings.app.mode.value,
        "market_open": market_open,
        "feed_age_sec": None if math.isinf(feed_age) else round(feed_age),
        "spot": trading_app._router.latest_spot,
        "vix": trading_app._router.latest_vix,
        "flagship_enabled": strategy is not None,
        "daily_state": journal.get_or_create_daily_state(),
        "risk_limits": {
            "max_trades_per_day": risk.max_trades_per_day,
            "daily_loss_limit_rs": risk.daily_loss_limit_rs,
            "max_consecutive_losses": risk.max_consecutive_losses,
        },
        "open_position": _with_live_leg_prices(trading_app, open_position) if open_position else None,
        "unrealized_pnl_rs": strategy.position_pnl_rs(open_position) if open_position else 0.0,
        "pending_request": journal.get_pending_direction_request(),
        "structure_preference": (
            strategy.structure_preference() if strategy is not None else journal.get_structure_preference()
        ),
        "entry_notice": strategy.entry_notice if strategy is not None else None,
        "oms_frozen": getattr(trading_app.oms, "frozen_reason", None),
        "exit_rules": {
            "sl_amount_rs": strat_cfg.exit.sl_amount_rs,
            "target_amount_rs": strat_cfg.exit.target_amount_rs,
            "trail_gap_rs": strat_cfg.exit.trail_gap_rs,
        },
        "buttons": {
            StructureType.DEBIT.value: {
                "expiry": strat_cfg.buying.expiry, "min_days_to_expiry": strat_cfg.buying.min_days_to_expiry,
            },
            StructureType.CREDIT.value: {
                "expiry": strat_cfg.selling.expiry, "min_days_to_expiry": strat_cfg.selling.min_days_to_expiry,
            },
        },
    }
