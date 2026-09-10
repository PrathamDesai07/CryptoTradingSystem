"""Atomic pre-trade capital reservations for risk-increasing Spot orders."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable
from uuid import uuid4


class PreTradeRiskError(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskReservation:
    reservation_id: str
    account_id: int | None
    asset: str
    amount: Decimal


class PreTradeRiskEngine:
    """Serialize balance checks and reserve capital until order acknowledgement."""

    def __init__(self, maximum_utilization_percent: Decimal) -> None:
        self._maximum = maximum_utilization_percent
        self._reserved: dict[tuple[int | None, str], Decimal] = {}
        self._uncertain: dict[str, RiskReservation] = {}
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        account_id: int | None,
        order: dict[str, str],
        rules: dict[str, Any],
        account_loader: Callable[[], Awaitable[dict[str, Any]]],
        price_loader: Callable[[str], Awaitable[Decimal]],
    ) -> RiskReservation | None:
        if order.get("side") != "BUY":
            return None
        quote_asset = str(rules.get("_quoteAsset") or "USDT")
        required = Decimal(order["quoteOrderQty"]) if "quoteOrderQty" in order else Decimal(order.get("quantity", "0")) * (Decimal(order["price"]) if "price" in order else await price_loader(order["symbol"]))
        if required <= 0:
            raise PreTradeRiskError("unable to calculate order balance utilization")
        async with self._lock:
            account = await account_loader()
            balance = next((item for item in account.get("balances", []) if item.get("asset") == quote_asset), None)
            exchange_free = Decimal(str(balance.get("free", "0"))) if balance else Decimal(0)
            key = (account_id, quote_asset)
            free = exchange_free - self._reserved.get(key, Decimal(0))
            if free <= 0:
                raise PreTradeRiskError(f"no unreserved {quote_asset} balance is available")
            utilization = required / free * Decimal(100)
            if utilization > self._maximum:
                raise PreTradeRiskError(f"risk guard rejected order: {utilization:.2f}% of unreserved {quote_asset} would be used (maximum {self._maximum:.2f}%)")
            reservation = RiskReservation(uuid4().hex, account_id, quote_asset, required)
            self._reserved[key] = self._reserved.get(key, Decimal(0)) + required
            return reservation

    async def release(self, reservation: RiskReservation | None) -> None:
        if reservation is None:
            return
        async with self._lock:
            self._uncertain.pop(reservation.reservation_id, None)
            key = (reservation.account_id, reservation.asset)
            remaining = self._reserved.get(key, Decimal(0)) - reservation.amount
            if remaining > 0:
                self._reserved[key] = remaining
            else:
                self._reserved.pop(key, None)

    async def mark_uncertain(self, reservation: RiskReservation | None) -> None:
        if reservation is None:
            return
        async with self._lock:
            self._uncertain[reservation.reservation_id] = reservation

    async def snapshot(self, account_id: int | None) -> dict[str, Any]:
        async with self._lock:
            return {
                "reserved": {asset: amount for (owner, asset), amount in self._reserved.items() if owner == account_id},
                "uncertain_orders": sum(1 for item in self._uncertain.values() if item.account_id == account_id),
            }
