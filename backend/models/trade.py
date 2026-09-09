"""Order and execution audit model."""

from pydantic import BaseModel, ConfigDict, model_validator

from .common import (
    ExitReason,
    OrderSide,
    PositiveDecimal,
    StrategyVariant,
    Symbol,
    TradeStatus,
    UtcDateTime,
)


class Trade(BaseModel):
    """A submitted order and its latest known execution state."""

    model_config = ConfigDict(validate_assignment=True)

    timestamp: UtcDateTime
    symbol: Symbol
    side: OrderSide
    quantity: PositiveDecimal
    price: PositiveDecimal | None = None
    binance_order_id: str | None = None
    client_order_id: str
    strategy_variant: StrategyVariant
    status: TradeStatus
    exit_reason: ExitReason | None = None

    @model_validator(mode="after")
    def validate_trade(self) -> "Trade":
        if not self.client_order_id.strip():
            raise ValueError("client_order_id cannot be empty")
        if self.status is TradeStatus.FILLED and self.price is None:
            raise ValueError("a filled trade must have an execution price")
        if self.side is OrderSide.BUY and self.exit_reason is not None:
            raise ValueError("an entry BUY trade cannot have an exit reason")
        return self
