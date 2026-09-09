"""O(1) latest-snapshot storage for partial order books."""

import asyncio

from models import PartialOrderBook


class OrderBookStore:
    def __init__(self) -> None:
        self._books: dict[str, PartialOrderBook] = {}
        self._lock = asyncio.Lock()

    async def update(self, book: PartialOrderBook) -> bool:
        async with self._lock:
            current = self._books.get(book.symbol)
            if current is not None and current.last_update_id >= book.last_update_id:
                return False
            self._books[book.symbol] = book
            return True

    async def get(self, symbol: str) -> PartialOrderBook | None:
        async with self._lock:
            return self._books.get(symbol.strip().upper())

    async def remove(self, symbol: str) -> None:
        async with self._lock:
            self._books.pop(symbol.strip().upper(), None)
