"""Resolve orders whose submission outcome is unknown without resubmitting them."""

import asyncio
from typing import Any, Awaitable, Callable


class ReconciliationService:
    def __init__(self, attempts: int = 3) -> None:
        self._attempts = attempts

    async def reconcile_unknown(
        self,
        order: dict[str, str],
        query: Callable[[str, str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        client_id = order.get("newClientOrderId")
        symbol = order.get("symbol")
        if not client_id or not symbol:
            return None
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(0.2 * attempt)
            try:
                result = await query(symbol, client_id)
            except Exception:
                continue
            if result and result.get("orderId") is not None:
                return result
        return None
