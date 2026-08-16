"""Abstract Strategy interface - the pluggable contract every strategy variant implements.

Phase 4 scope is signal-only: nothing here calls the OMS/broker directly. Methods return
typed intents (EntryIntent / ExitIntent) that the OMS (Phase 6) is responsible for actually
acting on - this keeps the decision logic fully unit-testable without a broker connection.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide, StructureType


@dataclass
class LegIntent:
    option_type: OptionType
    strike: float
    role: str  # "ITM" | "OTM"
    side: OrderSide
    token: str
    trading_symbol: str
    delta_at_selection: float | None = None


@dataclass
class EntryIntent:
    direction: Direction
    structure_type: StructureType
    expiry: str
    legs: list[LegIntent] = field(default_factory=list)
    iv_rank: float | None = None
    direction_request_id: int | None = None


@dataclass
class ExitIntent:
    reason: ExitReason


class Strategy(ABC):
    @abstractmethod
    def on_direction_request(self, direction: Direction) -> EntryIntent | None:
        """A manual Long/Short click from the dashboard. Returns an EntryIntent immediately
        if current MACD state already agrees with `direction`; otherwise the request is
        persisted as Pending (replacing any existing pending request) and this returns None -
        the entry fires later from on_market_data() once a matching crossover arrives."""
        ...

    @abstractmethod
    def on_market_data(self) -> EntryIntent | ExitIntent | None:
        """Called on every new candle close. Resolves a Pending direction request against
        the current MACD state if nothing is open, or evaluates exit conditions if a
        position is open. At most one intent per call - entry and exit are mutually
        exclusive since only one position can be open at a time."""
        ...

    @abstractmethod
    def cancel_pending_request(self) -> bool:
        """Dashboard Cancel button for a Pending request. Returns True if there was one."""
        ...

    @abstractmethod
    def manual_exit(self) -> ExitIntent | None:
        """Dashboard 'Exit Now' button - force-closes whatever is open, win or lose,
        bypassing every other exit rule. None if nothing is open."""
        ...

    @abstractmethod
    def on_square_off_trigger(self) -> ExitIntent | None:
        """Scheduler's mandatory square-off - hard backstop, not strategy-toggleable."""
        ...
