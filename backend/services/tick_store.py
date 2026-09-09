"""Concurrency-safe in-memory storage for the latest market ticks."""

import asyncio

from models import Tick


class TickStore:
    """Keep the most recent tick for each normalized symbol."""

    def __init__(self) -> None:
        self._ticks: dict[str, Tick] = {}
        self._lock = asyncio.Lock()

    async def update(self, tick: Tick) -> bool:
        """Store a tick unless a newer tick for the symbol already exists."""
        async with self._lock:
            existing = self._ticks.get(tick.symbol)
            if existing is not None:
                if existing.timestamp > tick.timestamp:
                    return False
                if existing == tick:
                    return False
            self._ticks[tick.symbol] = tick
            return True

    async def get(self, symbol: str) -> Tick | None:
        async with self._lock:
            return self._ticks.get(symbol.strip().upper())

    async def snapshot(self) -> dict[str, Tick]:
        async with self._lock:
            return dict(self._ticks)

    async def remove(self, symbol: str) -> None:
        async with self._lock:
            self._ticks.pop(symbol.strip().upper(), None)

    async def clear(self) -> None:
        async with self._lock:
            self._ticks.clear()
