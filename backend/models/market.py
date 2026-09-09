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
