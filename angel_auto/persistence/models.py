"""SQLAlchemy ORM models - the trade journal. Every table here exists to answer a concrete
question the rest of the system needs to ask:

- DailyRiskState: "have we hit 2 trades or the daily loss limit yet today?"
- DirectionRequest: "is there a Long/Short request pending MACD confirmation right now?"
- Position / Leg: "what's open, what legs make it up, what's its live P&L?"
- Order: "what did we actually send the broker for each leg, and what happened to it?"
- EquityCurve: dashboard P&L chart / backtest reporting.
- IVHistory: the 90-day India VIX window that IV Rank is computed from.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IVHistory(Base):
    """One row per trading day's India VIX close - the rolling window IV Rank is computed from."""

    __tablename__ = "iv_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    vix_close: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class StrategyPreference(Base):
    """Singleton row: your manual Buying/Selling (DEBIT/CREDIT) button preference, applied
    to the next entry whenever a direction request actually executes - not itself a
    trigger, and not MACD-gated like direction. The VIX-spike override in the strategy can
    still force a different structure than this preference at execution time."""

    __tablename__ = "strategy_preference"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    structure_type: Mapped[StructureType] = mapped_column(SAEnum(StructureType))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class DailyRiskState(Base):
    """One row per trading day - the running counters risk/circuit_breaker.py and
    risk/pretrade.py check before allowing any new entry."""

    __tablename__ = "daily_risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    trades_taken: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl_rs: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    trading_halted: Mapped[bool] = mapped_column(Boolean, default=False)
    halt_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class DirectionRequest(Base):
    """A manual Long/Short click from the dashboard - the only thing that can trigger an
    entry. Tracks whether it executed immediately, is waiting on a MACD crossover, was
    cancelled, or got replaced by a newer request (see strategy's Pending-request rules)."""

    __tablename__ = "direction_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[Direction] = mapped_column(SAEnum(Direction))
    status: Mapped[PendingRequestStatus] = mapped_column(SAEnum(PendingRequestStatus))
    macd_state_at_request: Mapped[str | None] = mapped_column(String(20), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    position: Mapped["Position | None"] = relationship(back_populates="direction_request", uselist=False)


class Position(Base):
    """One traded spread (both legs together) - the unit the strategy's fixed SL/trailing-
    target/manual-exit logic operates on (combined P&L, not per-leg)."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction_request_id: Mapped[int | None] = mapped_column(ForeignKey("direction_requests.id"), nullable=True)
    direction: Mapped[Direction] = mapped_column(SAEnum(Direction))
    structure_type: Mapped[StructureType] = mapped_column(SAEnum(StructureType))
    status: Mapped[PositionStatus] = mapped_column(SAEnum(PositionStatus))
    expiry: Mapped[str] = mapped_column(String(20))

    entry_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_reason: Mapped[ExitReason | None] = mapped_column(SAEnum(ExitReason), nullable=True)

    entry_net_premium_rs: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl_rs: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_profit_rs: Mapped[float] = mapped_column(Float, default=0.0)  # high-water mark for trailing stop
    trail_active: Mapped[bool] = mapped_column(Boolean, default=False)

    iv_rank_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)

    direction_request: Mapped["DirectionRequest | None"] = relationship(back_populates="position")
    legs: Mapped[list["Leg"]] = relationship(back_populates="position", cascade="all, delete-orphan")


class Leg(Base):
    """One option leg (ITM or OTM) of a Position."""

    __tablename__ = "legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))

    token: Mapped[str] = mapped_column(String(20))
    trading_symbol: Mapped[str] = mapped_column(String(50))
    option_type: Mapped[OptionType] = mapped_column(SAEnum(OptionType))
    strike: Mapped[float] = mapped_column(Float)
    role: Mapped[str] = mapped_column(String(10))  # "ITM" | "OTM"
    side: Mapped[OrderSide] = mapped_column(SAEnum(OrderSide))
    quantity: Mapped[int] = mapped_column(Integer)

    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_at_selection: Mapped[float | None] = mapped_column(Float, nullable=True)

    position: Mapped["Position"] = relationship(back_populates="legs")
    orders: Mapped[list["Order"]] = relationship(back_populates="leg", cascade="all, delete-orphan")


class Order(Base):
    """One broker order attempt for a Leg. A leg can have more than one row here if a
    fill-failure retry happens (see the order placement mechanics in the plan)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leg_id: Mapped[int] = mapped_column(ForeignKey("legs.id"))

    broker_order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    side: Mapped[OrderSide] = mapped_column(SAEnum(OrderSide))
    order_type: Mapped[str] = mapped_column(String(10))  # "LIMIT" | "MARKET" | "SL"
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus))
    is_safety_net: Mapped[bool] = mapped_column(Boolean, default=False)

    placed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    leg: Mapped["Leg"] = relationship(back_populates="orders")


class EquityCurve(Base):
    """Periodic equity snapshots - dashboard P&L chart and backtest reporting."""

    __tablename__ = "equity_curve"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    realized_pnl_rs: Mapped[float] = mapped_column(Float)
    unrealized_pnl_rs: Mapped[float] = mapped_column(Float)
    total_equity_rs: Mapped[float] = mapped_column(Float)
