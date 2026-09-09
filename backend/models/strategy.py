"""Strategy signal and position models."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from .common import (
    NonNegativeDecimal,
    PositiveDecimal,
    PositionStatus,
    SignalAction,
    StrategyVariant,
    Symbol,
    UtcDateTime,
)


class Signal(BaseModel):
    """A decision emitted by one strategy variant."""

    model_config = ConfigDict(frozen=True)

    symbol: Symbol
    variant: StrategyVariant
    action: SignalAction
    timestamp: UtcDateTime
    price: PositiveDecimal
    fast_value: PositiveDecimal
    slow_value: PositiveDecimal
    reason: str


class Position(BaseModel):
    """The independently tracked state of one strategy position."""

    model_config = ConfigDict(validate_assignment=True)

    symbol: Symbol
    variant: StrategyVariant
    status: PositionStatus
    quantity: PositiveDecimal
    entry_price: PositiveDecimal
    current_price: PositiveDecimal
    current_pnl: Decimal
    stop_loss_price: PositiveDecimal
    take_profit_price: PositiveDecimal
    opened_at: UtcDateTime
    closed_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def validate_position(self) -> "Position":
        if self.stop_loss_price >= self.entry_price:
            raise ValueError("a long position stop loss must be below entry price")
        if self.take_profit_price <= self.entry_price:
            raise ValueError("a long position take profit must be above entry price")
        if self.status is PositionStatus.OPEN and self.closed_at is not None:
            raise ValueError("an open position cannot have closed_at")
        if self.status is PositionStatus.CLOSED and self.closed_at is None:
            raise ValueError("a closed position must have closed_at")
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise ValueError("closed_at cannot be before opened_at")
        return self
