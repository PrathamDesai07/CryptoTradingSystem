"""Resilient Binance Spot Testnet trade-stream ingestion."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
import inspect
import json
import logging
import random
from time import monotonic
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from config import Settings
from models import OrderBookLevel, PartialOrderBook, Tick
from models.common import normalize_symbol
from services.order_book_store import OrderBookStore
from services.tick_store import TickStore

logger = logging.getLogger(__name__)

TickHandler = Callable[[Tick], None | Awaitable[None]]
OrderBookHandler = Callable[[PartialOrderBook], None | Awaitable[None]]


class ServerShutdownError(ConnectionError):
    """Raised when Binance announces an imminent stream-server shutdown."""


class BinanceStreamClient:
    """Manage subscriptions and continuously ingest Binance trade events."""

    def __init__(
        self,
        settings: Settings,
        tick_store: TickStore,
        on_tick: TickHandler | None = None,
        order_book_store: OrderBookStore | None = None,
        on_order_book: OrderBookHandler | None = None,
    ) -> None:
        self._settings = settings
        self._tick_store = tick_store
        self._on_tick = on_tick
        self._order_book_store = order_book_store
        self._on_order_book = on_order_book
        self._symbols = set(settings.symbols)
        self._depth_symbols: set[str] = set()
        self._symbols_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._websocket: ClientConnection | None = None
        self._request_id = 0
        self._last_control_sent_at = 0.0
        self._last_message_at: datetime | None = None

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set()

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    async def active_symbols(self) -> tuple[str, ...]:
        async with self._symbols_lock:
            return tuple(self._symbols)

    async def start(self) -> None:
        """Start the reconnecting stream loop once."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="binance-market-data")

    async def stop(self) -> None:
        """Close the socket and wait for the background task to finish."""
        self._stop_event.set()
        websocket = self._websocket
        if websocket is not None:
            await websocket.close()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._connected_event.clear()

    async def add_symbol(self, symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        async with self._symbols_lock:
            if normalized in self._symbols:
                return False
            if len(self._symbols) + len(self._depth_symbols) >= self._settings.websocket_max_streams:
                raise ValueError("maximum Binance stream subscription count reached")
            self._symbols.add(normalized)
        try:
            await self._send_streams("SUBSCRIBE", [self._ticker_stream(normalized)])
        except Exception:
            async with self._symbols_lock:
                self._symbols.discard(normalized)
            raise
        return True

    async def remove_symbol(self, symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        async with self._symbols_lock:
            if normalized not in self._symbols:
                return False
            self._symbols.remove(normalized)
            had_depth = normalized in self._depth_symbols
            self._depth_symbols.discard(normalized)
        streams = [self._ticker_stream(normalized)]
        if had_depth:
            streams.append(self._depth_stream(normalized))
        try:
            await self._send_streams("UNSUBSCRIBE", streams)
        except Exception:
            async with self._symbols_lock:
                self._symbols.add(normalized)
                if had_depth:
                    self._depth_symbols.add(normalized)
            raise
        await self._tick_store.remove(normalized)
        if self._order_book_store is not None:
            await self._order_book_store.remove(normalized)
        return True

    async def add_depth_symbol(self, symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        async with self._symbols_lock:
            if normalized not in self._symbols:
                raise ValueError("symbol must be active before subscribing to depth")
            if normalized in self._depth_symbols:
                return False
            if len(self._symbols) + len(self._depth_symbols) >= self._settings.websocket_max_streams:
                raise ValueError("maximum Binance stream subscription count reached")
            self._depth_symbols.add(normalized)
        try:
            await self._send_streams("SUBSCRIBE", [self._depth_stream(normalized)])
        except Exception:
            async with self._symbols_lock:
                self._depth_symbols.discard(normalized)
            raise
        return True

    async def remove_depth_symbol(self, symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        async with self._symbols_lock:
            if normalized not in self._depth_symbols:
                return False
            self._depth_symbols.remove(normalized)
        try:
            await self._send_streams("UNSUBSCRIBE", [self._depth_stream(normalized)])
        except Exception:
            async with self._symbols_lock:
                self._depth_symbols.add(normalized)
            raise
        if self._order_book_store is not None:
            await self._order_book_store.remove(normalized)
        return True

    async def _run(self) -> None:
        reconnect_attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                reconnect_attempt = 0
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, TimeoutError, ServerShutdownError) as error:
                if not self._stop_event.is_set():
                    logger.warning("binance_stream_disconnected error=%s", error)
            except Exception:
                if not self._stop_event.is_set():
                    logger.exception("binance_stream_unexpected_error")

            was_connected = self._connected_event.is_set()
            self._connected_event.clear()
            self._websocket = None
            if self._stop_event.is_set():
                break
            if was_connected:
                reconnect_attempt = 0

            base_delay = min(
                self._settings.websocket_reconnect_max_seconds,
                self._settings.websocket_reconnect_initial_seconds * (2**reconnect_attempt),
            )
            jitter = random.uniform(
                0,
                base_delay * self._settings.websocket_reconnect_jitter_ratio,
            )
            reconnect_attempt += 1
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=base_delay + jitter)
            except TimeoutError:
                pass

    async def _connect_once(self) -> None:
        async with connect(
            self._settings.binance_market_ws_url,
            open_timeout=self._settings.websocket_open_timeout_seconds,
            close_timeout=self._settings.websocket_close_timeout_seconds,
            ping_interval=self._settings.websocket_ping_interval_seconds,
            ping_timeout=self._settings.websocket_ping_timeout_seconds,
            max_size=self._settings.websocket_max_message_bytes,
            max_queue=self._settings.websocket_max_queue,
        ) as websocket:
            self._websocket = websocket
            self._connected_event.set()
            logger.info("binance_stream_connected")

            symbols = list(await self.active_symbols())
            if symbols:
                async with self._symbols_lock:
                    depth_symbols = tuple(self._depth_symbols)
                streams = [self._ticker_stream(symbol) for symbol in symbols]
                streams.extend(self._depth_stream(symbol) for symbol in depth_symbols)
                await self._send_streams("SUBSCRIBE", streams)

            while not self._stop_event.is_set():
                try:
                    raw_message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=self._settings.websocket_stale_timeout_seconds,
                    )
                except TimeoutError as error:
                    raise TimeoutError("Binance market stream became stale") from error
                self._last_message_at = datetime.now(UTC)
                await self._handle_message(raw_message)

    async def _handle_message(self, raw_message: str | bytes) -> None:
        try:
            payload = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            logger.warning("binance_stream_invalid_json")
            return

        if not isinstance(payload, dict):
            logger.warning("binance_stream_unexpected_payload_type")
            return
        if "id" in payload:
            if payload.get("result") is not None or "code" in payload:
                logger.warning("binance_subscription_response payload=%s", payload)
            return

        stream_name = payload.get("stream")
        event = payload.get("data", payload)
        if not isinstance(event, dict):
            logger.warning("binance_stream_invalid_event")
            return
        if event.get("e") == "serverShutdown":
            raise ServerShutdownError("Binance announced stream server shutdown")

        if isinstance(stream_name, str) and "@depth" in stream_name:
            try:
                book = self._parse_order_book(event, stream_name)
            except (KeyError, TypeError, ValueError, ArithmeticError, ValidationError):
                logger.warning("binance_depth_event_rejected payload=%s", event)
                return
            if self._order_book_store is not None:
                if not await self._order_book_store.update(book):
                    return
            if self._on_order_book is not None:
                try:
                    result = self._on_order_book(book)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("order_book_handler_failed symbol=%s", book.symbol)
            return

        try:
            tick = self._parse_tick(event)
        except (KeyError, TypeError, ValueError, ArithmeticError, ValidationError):
            logger.warning("binance_market_event_rejected payload=%s", event)
            return
        if tick is None:
            return

        if not await self._tick_store.update(tick):
            logger.debug("out_of_order_tick_ignored symbol=%s", tick.symbol)
            return
        if self._on_tick is not None:
            try:
                result = self._on_tick(tick)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("tick_handler_failed symbol=%s", tick.symbol)

    @staticmethod
    def _parse_tick(event: dict[str, Any]) -> Tick | None:
        event_type = event.get("e")
        if event_type == "trade":
            return Tick(
                symbol=event["s"],
                price=Decimal(event["p"]),
                quantity=Decimal(event["q"]),
                timestamp=datetime.fromtimestamp(event["T"] / 1000, tz=UTC),
            )

        if event_type == "24hrTicker":
            return Tick(
                symbol=event["s"],
                price=Decimal(event["c"]),
                quantity=Decimal(event["Q"]),
                timestamp=datetime.fromtimestamp(event["E"] / 1000, tz=UTC),
            )

        # Retain book-ticker compatibility for alternate YAML configurations.
        if event_type is None and {"s", "b", "B", "a", "A", "u"} <= event.keys():
            bid_price = Decimal(event["b"])
            ask_price = Decimal(event["a"])
            visible_quantity = Decimal(event["B"]) + Decimal(event["A"])
            return Tick(
                symbol=event["s"],
                price=(bid_price + ask_price) / 2,
                quantity=visible_quantity,
                timestamp=datetime.now(UTC),
            )
        return None

    def _parse_order_book(
        self, event: dict[str, Any], stream_name: str
    ) -> PartialOrderBook:
        symbol = stream_name.split("@", 1)[0].upper()
        bids = tuple(
            OrderBookLevel(price=price, quantity=quantity)
            for price, quantity in event["bids"][: self._settings.order_book_depth_levels]
            if Decimal(quantity) > 0
        )
        asks = tuple(
            OrderBookLevel(price=price, quantity=quantity)
            for price, quantity in event["asks"][: self._settings.order_book_depth_levels]
            if Decimal(quantity) > 0
        )
        return PartialOrderBook(
            symbol=symbol,
            last_update_id=event["lastUpdateId"],
            timestamp=datetime.now(UTC),
            bids=bids,
            asks=asks,
        )

    def _ticker_stream(self, symbol: str) -> str:
        return f"{symbol.lower()}{self._settings.market_stream_suffix}"

    def _depth_stream(self, symbol: str) -> str:
        return f"{symbol.lower()}{self._settings.order_book_stream_suffix}"

    async def _send_streams(self, method: str, streams: list[str]) -> None:
        websocket = self._websocket
        if websocket is None or not self.connected:
            return
        async with self._send_lock:
            elapsed = monotonic() - self._last_control_sent_at
            wait_time = self._settings.websocket_control_interval_seconds - elapsed
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._request_id += 1
            await websocket.send(
                json.dumps({"method": method, "params": streams, "id": self._request_id})
            )
            self._last_control_sent_at = monotonic()
