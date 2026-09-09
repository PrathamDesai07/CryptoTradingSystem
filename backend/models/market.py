"""Market-data models."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import NonNegativeDecimal, PositiveDecimal, Symbol, UtcDateTime


class Tick(BaseModel):
    """One normalized Binance trade update."""

    model_config = ConfigDict(frozen=True)

    symbol: Symbol
    price: PositiveDecimal
    quantity: PositiveDecimal
    timestamp: UtcDateTime


class Candle(BaseModel):
    """A finalized or in-progress one-minute OHLC candle."""

    model_config = ConfigDict(frozen=True)

    symbol: Symbol
    interval_start: UtcDateTime
    interval_end: UtcDateTime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    tick_count: int = Field(ge=1)
    volume: NonNegativeDecimal | None = None
    is_final: bool

    @model_validator(mode="after")
    def validate_candle(self) -> "Candle":
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must be after interval_start")
        if self.interval_start.second or self.interval_start.microsecond:
            raise ValueError("interval_start must be aligned to a minute boundary")
        if (self.interval_end - self.interval_start).total_seconds() != 60:
            raise ValueError("candle interval must be exactly one minute")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the greatest OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the smallest OHLC value")
        return self


class OrderBookLevel(BaseModel):
    """One price level in a partial order-book snapshot."""

    model_config = ConfigDict(frozen=True)

    price: PositiveDecimal
    quantity: NonNegativeDecimal


class PartialOrderBook(BaseModel):
    """The latest bounded set of best bid and ask levels for a symbol."""

    model_config = ConfigDict(frozen=True)

    symbol: Symbol
    last_update_id: int = Field(ge=0)
    timestamp: UtcDateTime
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]

    @model_validator(mode="after")
    def validate_ordering(self) -> "PartialOrderBook":
        if any(left.price < right.price for left, right in zip(self.bids, self.bids[1:])):
            raise ValueError("bids must be ordered from highest to lowest price")
        if any(left.price > right.price for left, right in zip(self.asks, self.asks[1:])):
            raise ValueError("asks must be ordered from lowest to highest price")
        return self
