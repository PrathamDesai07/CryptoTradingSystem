"""Shared domain types and validation helpers."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyVariant(StrEnum):
    A = "A"
    B = "B"


class SignalAction(StrEnum):
    BUY = "BUY"
    EXIT = "EXIT"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TradeStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


class ExitReason(StrEnum):
    SIGNAL = "SIGNAL"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"


def normalize_symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol may contain only letters and numbers")
    return symbol


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return value.astimezone(UTC)


Symbol = Annotated[str, BeforeValidator(normalize_symbol)]
UtcDateTime = Annotated[datetime, AfterValidator(require_utc)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
