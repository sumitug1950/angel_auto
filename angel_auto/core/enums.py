"""Shared enums used across strategy, risk, and OMS modules."""
from __future__ import annotations

from enum import Enum

from angel_auto.settings import Mode  # re-exported so callers only need core.enums

__all__ = [
    "Mode",
    "Direction",
    "StructureType",
    "OptionType",
    "OrderSide",
    "OrderStatus",
    "PendingRequestStatus",
    "PositionStatus",
    "ExitReason",
]


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class StructureType(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
    SINGLE_LEG = "SINGLE_LEG"  # the two automatic zero-cross strategies (ATM sell / ITM-offset buy)


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"       # created locally, not yet sent
    OPEN = "OPEN"              # sent to broker, not yet filled
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PendingRequestStatus(str, Enum):
    PENDING = "PENDING"        # waiting for MACD to confirm direction
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"      # superseded by a newer direction request


class PositionStatus(str, Enum):
    OPENING = "OPENING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"        # entry sequence failed before both legs were live


class ExitReason(str, Enum):
    FIXED_SL = "FIXED_SL"
    TRAILING_STOP = "TRAILING_STOP"
    OPPOSITE_MACD = "OPPOSITE_MACD"
    OPPOSITE_ZERO_CROSS = "OPPOSITE_ZERO_CROSS"  # tick-driven zero-cross strategies' exit
    MANUAL_EXIT = "MANUAL_EXIT"
    KILL_SWITCH = "KILL_SWITCH"
    SQUARE_OFF = "SQUARE_OFF"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
