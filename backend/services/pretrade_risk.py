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
    client_order_id: str
    symbol: str
    side: str


class PreTradeRiskEngine:
    """Serialize balance checks and reserve capital until order acknowledgement."""

    TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})

    def __init__(self, maximum_utilization_percent: Decimal, maximum_order_notional: Decimal | None = None,
                 repository: Any | None = None) -> None:
        self._maximum = maximum_utilization_percent
        self._maximum_order_notional = maximum_order_notional
        self._reserved: dict[tuple[int | None, str], Decimal] = {}
        self._uncertain: dict[str, RiskReservation] = {}
        self._locks: dict[tuple[int | None, str], asyncio.Lock] = {}
        self._repository = repository
        if repository is not None and hasattr(repository, "load_risk_reservations"):
            for reservation in repository.load_risk_reservations():
                self._restore(reservation)

    async def reserve(
        self,
        account_id: int | None,
        order: dict[str, str],
        rules: dict[str, Any],
        account_loader: Callable[[], Awaitable[dict[str, Any]]],
        price_loader: Callable[[str], Awaitable[Decimal]],
    ) -> RiskReservation | None:
        side = str(order.get("side") or "").upper()
        if side == "BUY":
            asset = str(rules.get("_quoteAsset") or "USDT")
            required = Decimal(order["quoteOrderQty"]) if "quoteOrderQty" in order else Decimal(order.get("quantity", "0")) * (Decimal(order["price"]) if "price" in order else await price_loader(order["symbol"]))
            notional = required
        elif side == "SELL":
            asset = str(rules.get("_baseAsset") or "")
            required = Decimal(order.get("quantity", "0"))
            notional = required * (Decimal(order["price"]) if "price" in order else await price_loader(order["symbol"]))
        else:
            return None
        if required <= 0:
            raise PreTradeRiskError("unable to calculate order balance utilization")
        if self._maximum_order_notional is not None and notional > self._maximum_order_notional:
            raise PreTradeRiskError(
                f"risk guard rejected order: notional {notional} exceeds maximum {self._maximum_order_notional}"
            )
        key = (account_id, asset)
        async with self._locks.setdefault(key, asyncio.Lock()):
            account = await account_loader()
            balance = next((item for item in account.get("balances", []) if item.get("asset") == asset), None)
            exchange_free = Decimal(str(balance.get("free", "0"))) if balance else Decimal(0)
            free = exchange_free - self._reserved.get(key, Decimal(0))
            if free <= 0:
                raise PreTradeRiskError(f"no unreserved {asset} balance is available")
            utilization = required / free * Decimal(100)
            if utilization > self._maximum:
                raise PreTradeRiskError(f"risk guard rejected order: {utilization:.2f}% of unreserved {asset} would be used (maximum {self._maximum:.2f}%)")
            reservation = RiskReservation(
                uuid4().hex, account_id, asset, required,
                str(order.get("newClientOrderId") or ""), order["symbol"], side,
            )
            self._reserved[key] = self._reserved.get(key, Decimal(0)) + required
            if self._repository is not None and hasattr(self._repository, "save_risk_reservation"):
                try:
                    await asyncio.to_thread(self._repository.save_risk_reservation, reservation, "PENDING")
                except Exception:
                    self._reserved[key] -= required
                    if self._reserved[key] <= 0:
                        self._reserved.pop(key, None)
                    raise
            return reservation

    async def release(self, reservation: RiskReservation | None) -> None:
        if reservation is None:
            return
        key = (reservation.account_id, reservation.asset)
        async with self._locks.setdefault(key, asyncio.Lock()):
            self._uncertain.pop(reservation.reservation_id, None)
            remaining = self._reserved.get(key, Decimal(0)) - reservation.amount
            if remaining > 0:
                self._reserved[key] = remaining
            else:
                self._reserved.pop(key, None)
            if self._repository is not None and hasattr(self._repository, "delete_risk_reservation"):
                await asyncio.to_thread(self._repository.delete_risk_reservation, reservation.reservation_id)

    async def mark_uncertain(self, reservation: RiskReservation | None) -> None:
        if reservation is None:
            return
        key = (reservation.account_id, reservation.asset)
        async with self._locks.setdefault(key, asyncio.Lock()):
            self._uncertain[reservation.reservation_id] = reservation
            if self._repository is not None and hasattr(self._repository, "save_risk_reservation"):
                await asyncio.to_thread(self._repository.save_risk_reservation, reservation, "UNKNOWN")

    async def acknowledge(self, reservation: RiskReservation | None, status: str) -> None:
        """Release terminal orders; retain and persist capital for live orders."""
        if reservation is None:
            return
        if status.upper() in self.TERMINAL_STATUSES:
            await self.release(reservation)
            return
        key = (reservation.account_id, reservation.asset)
        async with self._locks.setdefault(key, asyncio.Lock()):
            self._uncertain[reservation.reservation_id] = reservation
            if self._repository is not None and hasattr(self._repository, "save_risk_reservation"):
                await asyncio.to_thread(self._repository.save_risk_reservation, reservation, status.upper() or "OPEN")

    async def snapshot(self, account_id: int | None) -> dict[str, Any]:
        return {
            "reserved": {asset: amount for (owner, asset), amount in tuple(self._reserved.items()) if owner == account_id},
            "uncertain_orders": sum(1 for item in tuple(self._uncertain.values()) if item.account_id == account_id),
        }

    def pending(self, account_id: int | None) -> tuple[RiskReservation, ...]:
        return tuple(item for item in tuple(self._uncertain.values()) if item.account_id == account_id)

    async def acknowledge_client(self, account_id: int | None, client_order_id: str, status: str) -> None:
        for reservation in self.pending(account_id):
            if reservation.client_order_id == client_order_id:
                await self.acknowledge(reservation, status)
                return

    def _restore(self, item: dict[str, Any]) -> None:
        reservation = RiskReservation(
            str(item["reservation_id"]), item.get("account_id"), str(item["asset"]),
            Decimal(str(item["amount"])), str(item.get("client_order_id") or ""),
            str(item["symbol"]), str(item["side"]),
        )
        key = (reservation.account_id, reservation.asset)
        self._reserved[key] = self._reserved.get(key, Decimal(0)) + reservation.amount
        self._uncertain[reservation.reservation_id] = reservation
