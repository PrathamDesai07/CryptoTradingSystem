"""Real-time, minute-aligned OHLC aggregation with bounded history."""

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import json
import logging
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from models import Candle, Tick

logger = logging.getLogger(__name__)
CandleHandler = Callable[[Candle], None | Awaitable[None]]


class CandleService:
    """Aggregate ticks with expected O(1) work per update."""

    def __init__(
        self,
        history_size: int,
        interval_seconds: int,
        on_candle: CandleHandler | None = None,
    ) -> None:
        self._history_size = history_size
        self._interval_seconds = interval_seconds
        self._on_candle = on_candle
        self._current: dict[str, Candle] = {}
        self._history: dict[str, deque[Candle]] = defaultdict(
            lambda: deque(maxlen=self._history_size)
        )
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._boundary_loop(), name="candle-boundaries")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def process_tick(self, tick: Tick) -> None:
        interval_start = self._bucket_start(tick.timestamp)
        interval_end = interval_start + timedelta(seconds=self._interval_seconds)
        notifications: list[Candle] = []

        async with self._lock:
            history = self._history[tick.symbol]
            last = history[-1] if history else None
            if last is not None and interval_start <= last.interval_start:
                logger.debug("late_tick_ignored symbol=%s", tick.symbol)
                return
            current = self._current.get(tick.symbol)
            if current is not None and interval_start > current.interval_start:
                finalized = current.model_copy(update={"is_final": True})
                history.append(finalized)
                notifications.append(finalized)
                last = finalized
                current = None

            if last is not None:
                self._append_gap_fillers(tick.symbol, last, interval_start, notifications)

            if current is None:
                current = Candle(
                    symbol=tick.symbol,
                    interval_start=interval_start,
                    interval_end=interval_end,
                    open=tick.price,
                    high=tick.price,
                    low=tick.price,
                    close=tick.price,
                    tick_count=1,
                    volume=None,
                    is_final=False,
                )
            else:
                current = current.model_copy(
                    update={
                        "high": max(current.high, tick.price),
                        "low": min(current.low, tick.price),
                        "close": tick.price,
                        "tick_count": current.tick_count + 1,
                    }
                )
            self._current[tick.symbol] = current
            notifications.append(current)

        for candle in notifications:
            await self._notify(candle)

    def _append_gap_fillers(
        self,
        symbol: str,
        last: Candle,
        target_start: datetime,
        notifications: list[Candle],
    ) -> None:
        """Keep history contiguous by filling minutes that received no ticks.

        Binance's own kline series never has holes, and the SMA/EMA window counts
        samples rather than minutes, so skipped minutes would silently misalign it.
        Fillers carry the previous close forward and therefore cannot create a
        crossover on their own.
        """
        step = timedelta(seconds=self._interval_seconds)
        cursor = last.interval_start + step
        filled = 0
        while cursor < target_start and filled < self._history_size:
            filler = Candle(
                symbol=symbol,
                interval_start=cursor,
                interval_end=cursor + step,
                open=last.close,
                high=last.close,
                low=last.close,
                close=last.close,
                tick_count=0,
                volume=None,
                is_final=True,
            )
            self._history[symbol].append(filler)
            notifications.append(filler)
            cursor += step
            filled += 1
        if filled:
            logger.info("candle_gap_filled symbol=%s count=%s", symbol, filled)

    async def current(self, symbol: str) -> Candle | None:
        async with self._lock:
            return self._current.get(symbol.strip().upper())

    async def history(self, symbol: str, limit: int) -> tuple[Candle, ...]:
        async with self._lock:
            history = self._history.get(symbol.strip().upper())
            if history is None:
                return ()
            return tuple(list(history)[-limit:])

    async def bootstrap_history(
        self,
        symbol: str,
        rest_url: str,
        interval_name: str,
        limit: int,
        timeout_seconds: float,
    ) -> None:
        """Seed finalized history from Binance's public kline REST endpoint."""
        try:
            payload = await asyncio.to_thread(
                self._request_klines,
                symbol,
                rest_url,
                interval_name,
                min(limit + 1, 1000),
                timeout_seconds,
            )
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            candles = [
                Candle(
                    symbol=symbol,
                    interval_start=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                    interval_end=datetime.fromtimestamp((row[6] + 1) / 1000, tz=UTC),
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[5]),
                    tick_count=int(row[8]),
                    is_final=True,
                )
                for row in payload
                if row[6] < now_ms
            ][-limit:]
            async with self._lock:
                history = self._history[symbol]
                history.clear()
                history.extend(candles)
            logger.info("candle_history_loaded symbol=%s count=%s", symbol, len(candles))
        except Exception:
            logger.exception("candle_history_load_failed symbol=%s", symbol)

    async def chart_history(
        self,
        symbol: str,
        rest_url: str,
        interval_name: str,
        limit: int,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], ...]:
        """Fetch chart-only Binance candles for a requested display interval."""
        payload = await asyncio.to_thread(
            self._request_klines,
            symbol,
            rest_url,
            interval_name,
            limit,
            timeout_seconds,
        )
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return tuple(
            {
                "symbol": symbol,
                "interval_start": datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                "interval_end": datetime.fromtimestamp((row[6] + 1) / 1000, tz=UTC),
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
                "tick_count": max(1, int(row[8])),
                "is_final": row[6] < now_ms,
            }
            for row in payload
        )

    @staticmethod
    def _request_klines(
        symbol: str,
        rest_url: str,
        interval_name: str,
        limit: int,
        timeout_seconds: float,
    ) -> list[list[Any]]:
        query = urlencode({"symbol": symbol, "interval": interval_name, "limit": limit})
        url = f"{rest_url.rstrip('/')}/api/v3/klines?{query}"
        with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, list):
            raise ValueError("Binance kline response must be a list")
        return payload

    async def remove_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        async with self._lock:
            self._current.pop(normalized, None)
            self._history.pop(normalized, None)

    def _bucket_start(self, timestamp: datetime) -> datetime:
        epoch_seconds = int(timestamp.timestamp())
        bucket_seconds = epoch_seconds - (epoch_seconds % self._interval_seconds)
        return datetime.fromtimestamp(bucket_seconds, tz=UTC)

    async def _boundary_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(UTC)
            next_boundary = self._bucket_start(now) + timedelta(
                seconds=self._interval_seconds
            )
            delay = max(0, (next_boundary - now).total_seconds())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                pass

            finalized = await self._finalize_due(datetime.now(UTC))
            for candle in finalized:
                await self._notify(candle)

    async def _finalize_due(self, boundary: datetime) -> list[Candle]:
        """Finalize every in-progress candle whose interval has elapsed."""
        finalized: list[Candle] = []
        async with self._lock:
            for symbol, candle in tuple(self._current.items()):
                if candle.interval_end <= boundary:
                    closed = candle.model_copy(update={"is_final": True})
                    self._history[symbol].append(closed)
                    self._current.pop(symbol, None)
                    finalized.append(closed)
        return finalized

    async def _notify(self, candle: Candle) -> None:
        if self._on_candle is None:
            return
        try:
            result = self._on_candle(candle)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("candle_handler_failed symbol=%s", candle.symbol)
