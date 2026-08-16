"""Write/read helpers over the ORM models. Everything returns plain primitives/dicts
rather than live ORM objects, so callers never hit SQLAlchemy detached-instance issues
after the writing session_scope() has closed - query fresh when you need current state.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from angel_auto.core.enums import (
    Direction,
    ExitReason,
    OptionType,
    OrderSide,
    OrderStatus,
    PendingRequestStatus,
    PositionStatus,
    StructureType,
)
from angel_auto.persistence.db import session_scope
from angel_auto.persistence.models import (
    DailyRiskState,
    DirectionRequest,
    EquityCurve,
    IVHistory,
    Leg,
    Order,
    Position,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# --- IV History (India VIX bootstrap + daily rolling window) ---------------


def upsert_vix_close(trade_date: date, vix_close: float) -> None:
    with session_scope() as session:
        existing = session.scalar(select(IVHistory).where(IVHistory.trade_date == trade_date))
        if existing:
            existing.vix_close = vix_close
        else:
            session.add(IVHistory(trade_date=trade_date, vix_close=vix_close))


def get_vix_history(lookback_days: int, as_of: date | None = None) -> list[float]:
    as_of = as_of or date.today()
    with session_scope() as session:
        rows = session.scalars(
            select(IVHistory)
            .where(IVHistory.trade_date <= as_of)
            .order_by(IVHistory.trade_date.desc())
            .limit(lookback_days)
        ).all()
        return [row.vix_close for row in rows]


# --- Daily risk state (max_trades_per_day, daily_loss_limit_rs, kill state) ----


def get_or_create_daily_state(trade_date: date | None = None) -> dict:
    trade_date = trade_date or date.today()
    with session_scope() as session:
        state = session.scalar(select(DailyRiskState).where(DailyRiskState.trade_date == trade_date))
        if state is None:
            state = DailyRiskState(trade_date=trade_date)
            session.add(state)
            session.flush()
        return _daily_state_to_dict(state)


def increment_daily_trade_count(trade_date: date | None = None) -> int:
    trade_date = trade_date or date.today()
    with session_scope() as session:
        state = session.scalar(select(DailyRiskState).where(DailyRiskState.trade_date == trade_date))
        if state is None:
            state = DailyRiskState(trade_date=trade_date)
            session.add(state)
            session.flush()
        state.trades_taken += 1
        return state.trades_taken


def record_trade_pnl(realized_pnl_delta_rs: float, trade_date: date | None = None) -> dict:
    """Apply a closed trade's P&L to the daily counters. Updates consecutive_losses too
    (resets to 0 on a win, increments on a loss) - circuit_breaker.py reads this."""
    trade_date = trade_date or date.today()
    with session_scope() as session:
        state = session.scalar(select(DailyRiskState).where(DailyRiskState.trade_date == trade_date))
        if state is None:
            state = DailyRiskState(trade_date=trade_date)
            session.add(state)
            session.flush()
        state.realized_pnl_rs += realized_pnl_delta_rs
        state.consecutive_losses = 0 if realized_pnl_delta_rs > 0 else state.consecutive_losses + 1
        session.flush()
        return _daily_state_to_dict(state)


def set_trading_halt(reason: str, trade_date: date | None = None) -> None:
    trade_date = trade_date or date.today()
    with session_scope() as session:
        state = session.scalar(select(DailyRiskState).where(DailyRiskState.trade_date == trade_date))
        if state is None:
            state = DailyRiskState(trade_date=trade_date)
            session.add(state)
        state.trading_halted = True
        state.halt_reason = reason


def _daily_state_to_dict(state: DailyRiskState) -> dict:
    return {
        "trade_date": state.trade_date,
        "trades_taken": state.trades_taken,
        "realized_pnl_rs": state.realized_pnl_rs,
        "consecutive_losses": state.consecutive_losses,
        "trading_halted": state.trading_halted,
        "halt_reason": state.halt_reason,
    }


# --- Direction requests (manual Long/Short, MACD-gated) ---------------------


def create_direction_request(direction: Direction, macd_state_at_request: str) -> int:
    with session_scope() as session:
        request = DirectionRequest(
            direction=direction,
            status=PendingRequestStatus.PENDING,
            macd_state_at_request=macd_state_at_request,
        )
        session.add(request)
        session.flush()
        return request.id


def resolve_direction_request(request_id: int, status: PendingRequestStatus) -> None:
    with session_scope() as session:
        request = session.get(DirectionRequest, request_id)
        if request is not None:
            request.status = status
            request.resolved_at = _utcnow()


def get_pending_direction_request() -> dict | None:
    with session_scope() as session:
        request = session.scalar(
            select(DirectionRequest)
            .where(DirectionRequest.status == PendingRequestStatus.PENDING)
            .order_by(DirectionRequest.requested_at.desc())
        )
        if request is None:
            return None
        return {
            "id": request.id,
            "direction": request.direction,
            "requested_at": request.requested_at,
            "macd_state_at_request": request.macd_state_at_request,
        }


# --- Positions / legs / orders ----------------------------------------------


def create_position(
    direction: Direction,
    structure_type: StructureType,
    expiry: str,
    direction_request_id: int | None = None,
    iv_rank_at_entry: float | None = None,
) -> int:
    with session_scope() as session:
        position = Position(
            direction_request_id=direction_request_id,
            direction=direction,
            structure_type=structure_type,
            status=PositionStatus.OPENING,
            expiry=expiry,
            iv_rank_at_entry=iv_rank_at_entry,
        )
        session.add(position)
        session.flush()
        return position.id


def add_leg(
    position_id: int,
    token: str,
    trading_symbol: str,
    option_type: OptionType,
    strike: float,
    role: str,
    side: OrderSide,
    quantity: int,
    delta_at_selection: float | None = None,
) -> int:
    with session_scope() as session:
        leg = Leg(
            position_id=position_id,
            token=token,
            trading_symbol=trading_symbol,
            option_type=option_type,
            strike=strike,
            role=role,
            side=side,
            quantity=quantity,
            delta_at_selection=delta_at_selection,
        )
        session.add(leg)
        session.flush()
        return leg.id


def add_order(
    leg_id: int,
    side: OrderSide,
    order_type: str,
    quantity: int,
    price: float | None = None,
    trigger_price: float | None = None,
    is_safety_net: bool = False,
) -> int:
    with session_scope() as session:
        order = Order(
            leg_id=leg_id,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            status=OrderStatus.PENDING,
            is_safety_net=is_safety_net,
        )
        session.add(order)
        session.flush()
        return order.id


def update_order_status(
    order_id: int,
    status: OrderStatus,
    broker_order_id: str | None = None,
    filled_price: float | None = None,
    reject_reason: str | None = None,
) -> None:
    with session_scope() as session:
        order = session.get(Order, order_id)
        if order is None:
            return
        order.status = status
        if broker_order_id is not None:
            order.broker_order_id = broker_order_id
        if status == OrderStatus.FILLED:
            order.filled_at = _utcnow()
            order.filled_price = filled_price
        if reject_reason is not None:
            order.reject_reason = reject_reason


def update_leg_fill(leg_id: int, entry_price: float | None = None, exit_price: float | None = None) -> None:
    with session_scope() as session:
        leg = session.get(Leg, leg_id)
        if leg is None:
            return
        if entry_price is not None:
            leg.entry_price = entry_price
        if exit_price is not None:
            leg.exit_price = exit_price


def update_position_status(position_id: int, status: PositionStatus, set_entry_time: bool = False) -> None:
    with session_scope() as session:
        position = session.get(Position, position_id)
        if position is None:
            return
        position.status = status
        if set_entry_time:
            position.entry_time = _utcnow()


def update_trailing_peak(position_id: int, peak_profit_rs: float, trail_active: bool = True) -> None:
    with session_scope() as session:
        position = session.get(Position, position_id)
        if position is None:
            return
        position.peak_profit_rs = peak_profit_rs
        position.trail_active = trail_active


def close_position(position_id: int, exit_reason: ExitReason, realized_pnl_rs: float) -> None:
    with session_scope() as session:
        position = session.get(Position, position_id)
        if position is None:
            return
        position.status = PositionStatus.CLOSED
        position.exit_time = _utcnow()
        position.exit_reason = exit_reason
        position.realized_pnl_rs = realized_pnl_rs
    record_trade_pnl(realized_pnl_rs)


def get_open_position() -> dict | None:
    """At most one open position at a time (max_concurrent_positions=1)."""
    with session_scope() as session:
        position = session.scalar(
            select(Position).where(Position.status.in_([PositionStatus.OPENING, PositionStatus.OPEN]))
        )
        if position is None:
            return None
        legs = [
            {
                "id": leg.id,
                "token": leg.token,
                "trading_symbol": leg.trading_symbol,
                "option_type": leg.option_type,
                "strike": leg.strike,
                "role": leg.role,
                "side": leg.side,
                "quantity": leg.quantity,
                "entry_price": leg.entry_price,
            }
            for leg in position.legs
        ]
        return {
            "id": position.id,
            "direction": position.direction,
            "structure_type": position.structure_type,
            "status": position.status,
            "expiry": position.expiry,
            "entry_time": position.entry_time,
            "peak_profit_rs": position.peak_profit_rs,
            "trail_active": position.trail_active,
            "legs": legs,
        }


def list_recent_positions(limit: int = 50) -> list[dict]:
    """Closed positions, most recent first - the dashboard trade log."""
    with session_scope() as session:
        positions = session.scalars(
            select(Position)
            .where(Position.status == PositionStatus.CLOSED)
            .order_by(Position.exit_time.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": p.id,
                "direction": p.direction,
                "structure_type": p.structure_type,
                "expiry": p.expiry,
                "entry_time": p.entry_time,
                "exit_time": p.exit_time,
                "exit_reason": p.exit_reason,
                "realized_pnl_rs": p.realized_pnl_rs,
                "iv_rank_at_entry": p.iv_rank_at_entry,
                "legs": [
                    {"role": leg.role, "strike": leg.strike, "option_type": leg.option_type, "side": leg.side}
                    for leg in p.legs
                ],
            }
            for p in positions
        ]


# --- Equity curve -------------------------------------------------------


def append_equity_point(realized_pnl_rs: float, unrealized_pnl_rs: float, total_equity_rs: float) -> None:
    with session_scope() as session:
        session.add(
            EquityCurve(
                realized_pnl_rs=realized_pnl_rs,
                unrealized_pnl_rs=unrealized_pnl_rs,
                total_equity_rs=total_equity_rs,
            )
        )


def get_equity_curve(limit: int = 500) -> list[dict]:
    with session_scope() as session:
        points = session.scalars(select(EquityCurve).order_by(EquityCurve.timestamp.desc()).limit(limit)).all()
        return [
            {
                "timestamp": p.timestamp,
                "realized_pnl_rs": p.realized_pnl_rs,
                "unrealized_pnl_rs": p.unrealized_pnl_rs,
                "total_equity_rs": p.total_equity_rs,
            }
            for p in reversed(points)
        ]
