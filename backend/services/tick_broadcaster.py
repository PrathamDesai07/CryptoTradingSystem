"""Fan live ticks out to connected frontend WebSocket clients."""

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from models import Candle, IndicatorPoint, PartialOrderBook, Signal, Tick

logger = logging.getLogger(__name__)


class TickBroadcaster:
    """Coalesce high-frequency ticks and fan out only the latest values."""

    def __init__(self, interval_milliseconds: int) -> None:
        self._clients: set[WebSocket] = set()
        self._pending: dict[str, Tick] = {}
        self._pending_candles: dict[str, Candle] = {}
        self._pending_books: dict[str, PartialOrderBook] = {}
        self._pending_indicators: dict[str, IndicatorPoint] = {}
        self._pending_signals: dict[tuple[str, str], Signal] = {}
        self._depth_clients: dict[str, set[WebSocket]] = {}
        self._client_depth_symbols: dict[WebSocket, set[str]] = {}
        self._interval_seconds = interval_milliseconds / 1000
        self._event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        for symbol in self._client_depth_symbols.pop(websocket, set()):
            clients = self._depth_clients.get(symbol)
            if clients is None:
                continue
            clients.discard(websocket)
            if not clients:
                self._depth_clients.pop(symbol, None)

    def subscribe_depth(self, websocket: WebSocket, symbol: str) -> bool:
        """Return True when this is the first viewer for a symbol."""
        clients = self._depth_clients.setdefault(symbol, set())
        was_empty = not clients
        clients.add(websocket)
        self._client_depth_symbols.setdefault(websocket, set()).add(symbol)
        return was_empty

    def unsubscribe_depth(self, websocket: WebSocket, symbol: str) -> bool:
        """Return True when no viewers remain for a symbol."""
        clients = self._depth_clients.get(symbol)
        if clients is None:
            return False
        clients.discard(websocket)
        client_symbols = self._client_depth_symbols.get(websocket)
        if client_symbols is not None:
            client_symbols.discard(symbol)
            if not client_symbols:
                self._client_depth_symbols.pop(websocket, None)
        if clients:
            return False
        self._depth_clients.pop(symbol, None)
        return True

    def depth_symbols_for(self, websocket: WebSocket) -> tuple[str, ...]:
        return tuple(self._client_depth_symbols.get(websocket, ()))

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="frontend-tick-broadcaster")

    async def stop(self) -> None:
        self._stop_event.set()
        self._event.set()
        if self._task is not None:
            await self._task
        self._task = None

    def publish(self, tick: Tick) -> None:
        """Replace a symbol's pending tick in expected O(1) time."""
        self._pending[tick.symbol] = tick
        self._event.set()

    def publish_candle(self, candle: Candle) -> None:
        self._pending_candles[candle.symbol] = candle
        self._event.set()

    def publish_order_book(self, book: PartialOrderBook) -> None:
        self._pending_books[book.symbol] = book
        self._event.set()

    def publish_indicator(self, indicator: IndicatorPoint) -> None:
        self._pending_indicators[indicator.symbol] = indicator
        self._event.set()

    def publish_signal(self, signal: Signal) -> None:
        self._pending_signals[(signal.symbol, signal.variant.value)] = signal
        self._event.set()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self._event.wait()
            self._event.clear()
            await asyncio.sleep(self._interval_seconds)
            if self._stop_event.is_set():
                break
            ticks = tuple(self._pending.values())
            candles = tuple(self._pending_candles.values())
            books = tuple(self._pending_books.values())
            indicators = tuple(self._pending_indicators.values())
            signals = tuple(self._pending_signals.values())
            self._pending.clear()
            self._pending_candles.clear()
            self._pending_books.clear()
            self._pending_indicators.clear()
            self._pending_signals.clear()
            if ticks:
                await self._broadcast_all("ticks", ticks)
            if candles:
                await self._broadcast_all("candles", candles)
            for book in books:
                await self._broadcast_book(book)
            if indicators:
                await self._broadcast_all("indicators", indicators)
            if signals:
                await self._broadcast_all("signals", signals)

    async def _broadcast_all(self, message_type: str, items: tuple[Any, ...]) -> None:
        clients = tuple(self._clients)
        if not clients:
            return

        payload = {
            "type": message_type,
            "data": [item.model_dump(mode="json") for item in items],
        }
        await self._send_to_clients(clients, payload)

    async def _broadcast_book(self, book: PartialOrderBook) -> None:
        clients = tuple(self._depth_clients.get(book.symbol, ()))
        if not clients:
            return
        await self._send_to_clients(
            clients,
            {"type": "order_book", "data": book.model_dump(mode="json")},
        )

    async def _send_to_clients(
        self, clients: tuple[WebSocket, ...], payload: dict[str, Any]
    ) -> None:
        results = await asyncio.gather(
            *(client.send_json(payload) for client in clients),
            return_exceptions=True,
        )
        failed = [
            client for client, result in zip(clients, results, strict=True)
            if isinstance(result, Exception)
        ]
        if failed:
            for client in failed:
                self._clients.discard(client)
            logger.info("stale_frontend_clients_removed count=%s", len(failed))
