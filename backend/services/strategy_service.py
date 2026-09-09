"""Finalized-candle SMA/EMA crossover strategy."""

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
import inspect
import logging

from models import Candle, IndicatorPoint, Signal, SignalAction, StrategyVariant

logger = logging.getLogger(__name__)
SignalHandler = Callable[[Signal], None | Awaitable[None]]
IndicatorHandler = Callable[[IndicatorPoint], None | Awaitable[None]]


class StrategyService:
    """Maintain O(1) rolling SMA/EMA state per symbol."""

    def __init__(
        self,
        fast_period: int,
        slow_period: int,
        signal_history_size: int,
        indicator_history_size: int,
        on_signal: SignalHandler | None = None,
        on_indicator: IndicatorHandler | None = None,
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self._alpha = Decimal(2) / Decimal(slow_period + 1)
        self._closes: dict[str, deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self.fast_period)
        )
        self._sums: dict[str, Decimal] = defaultdict(Decimal)
        self._emas: dict[str, Decimal] = {}
        self._sample_counts: dict[str, int] = defaultdict(int)
        self._relations: dict[str, int] = {}
        self._indicators: dict[str, deque[IndicatorPoint]] = defaultdict(
            lambda: deque(maxlen=indicator_history_size)
        )
        self._signals: deque[Signal] = deque(maxlen=signal_history_size)
        self._latest_signals: dict[tuple[str, StrategyVariant], Signal] = {}
        self._on_signal = on_signal
        self._on_indicator = on_indicator
        self._lock = asyncio.Lock()

    async def seed(self, symbol: str, candles: Iterable[Candle]) -> None:
        """Build indicator state from finalized history without emitting signals."""
        async with self._lock:
            self._reset_symbol(symbol)
            for candle in candles:
                if candle.is_final:
                    self._calculate(candle)

    async def process_candle(self, candle: Candle) -> tuple[Signal, ...]:
        """Evaluate one finalized candle and emit only genuine crossovers."""
        if not candle.is_final:
            return ()
        async with self._lock:
            previous_relation = self._relations.get(candle.symbol)
            indicator = self._calculate(candle)
            if indicator.sma is None or indicator.ema is None:
                signals: tuple[Signal, ...] = ()
            else:
                relation = self._compare(indicator.sma, indicator.ema)
                action: SignalAction | None = None
                if previous_relation is not None and previous_relation <= 0 < relation:
                    action = SignalAction.BUY
                elif previous_relation is not None and previous_relation >= 0 > relation:
                    action = SignalAction.EXIT
                if action is None:
                    signals = ()
                else:
                    reason = (
                        f"SMA({self.fast_period}) crossed "
                        f"{'above' if action is SignalAction.BUY else 'below'} "
                        f"EMA({self.slow_period})"
                    )
                    signals = tuple(
                        Signal(
                            symbol=candle.symbol,
                            variant=variant,
                            action=action,
                            timestamp=candle.interval_end,
                            price=candle.close,
                            fast_value=indicator.sma,
                            slow_value=indicator.ema,
                            reason=reason,
                        )
                        for variant in StrategyVariant
                    )
                    for signal in signals:
                        self._signals.append(signal)
                        self._latest_signals[(signal.symbol, signal.variant)] = signal

        await self._notify_indicator(indicator)
        for signal in signals:
            await self._notify_signal(signal)
        return signals

    async def indicators(self, symbol: str, limit: int) -> tuple[IndicatorPoint, ...]:
        async with self._lock:
            points = self._indicators.get(symbol.strip().upper())
            return () if points is None else tuple(list(points)[-limit:])

    async def signals(
        self, symbol: str | None = None, limit: int = 100
    ) -> tuple[Signal, ...]:
        normalized = symbol.strip().upper() if symbol else None
        async with self._lock:
            selected = (
                signal for signal in self._signals
                if normalized is None or signal.symbol == normalized
            )
            return tuple(list(selected)[-limit:])

    async def latest_signals(self, symbol: str) -> dict[StrategyVariant, Signal]:
        normalized = symbol.strip().upper()
        async with self._lock:
            return {
                variant: signal
                for (signal_symbol, variant), signal in self._latest_signals.items()
                if signal_symbol == normalized
            }

    async def remove_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        async with self._lock:
            self._reset_symbol(normalized)
            self._indicators.pop(normalized, None)
            for key in tuple(self._latest_signals):
                if key[0] == normalized:
                    self._latest_signals.pop(key, None)

    def _calculate(self, candle: Candle) -> IndicatorPoint:
        symbol = candle.symbol
        closes = self._closes[symbol]
        if len(closes) == self.fast_period:
            self._sums[symbol] -= closes[0]
        closes.append(candle.close)
        self._sums[symbol] += candle.close
        self._sample_counts[symbol] += 1

        previous_ema = self._emas.get(symbol)
        ema = (
            candle.close
            if previous_ema is None
            else (candle.close - previous_ema) * self._alpha + previous_ema
        )
        self._emas[symbol] = ema
        ready = self._sample_counts[symbol] >= self.slow_period
        sma = self._sums[symbol] / Decimal(self.fast_period) if len(closes) == self.fast_period else None
        point = IndicatorPoint(
            symbol=symbol,
            timestamp=candle.interval_start,
            close=candle.close,
            sma=sma if ready else None,
            ema=ema if ready else None,
            fast_period=self.fast_period,
            slow_period=self.slow_period,
        )
        self._indicators[symbol].append(point)
        if point.sma is not None and point.ema is not None:
            self._relations[symbol] = self._compare(point.sma, point.ema)
        return point

    def _reset_symbol(self, symbol: str) -> None:
        self._closes.pop(symbol, None)
        self._sums.pop(symbol, None)
        self._emas.pop(symbol, None)
        self._sample_counts.pop(symbol, None)
        self._relations.pop(symbol, None)
        self._indicators.pop(symbol, None)

    @staticmethod
    def _compare(left: Decimal, right: Decimal) -> int:
        return (left > right) - (left < right)

    async def _notify_signal(self, signal: Signal) -> None:
        await self._notify(self._on_signal, signal, "signal_handler_failed")

    async def _notify_indicator(self, indicator: IndicatorPoint) -> None:
        await self._notify(self._on_indicator, indicator, "indicator_handler_failed")

    @staticmethod
    async def _notify(handler: Callable | None, value: object, error: str) -> None:
        if handler is None:
            return
        try:
            result = handler(value)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("%s", error)
