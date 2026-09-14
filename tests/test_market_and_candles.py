import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from models import Candle, Tick
from services.candle_service import CandleService
from services.market_data import BinanceStreamClient
from services.tick_store import TickStore


class MarketParsingTests(unittest.TestCase):
    def test_ticker_uses_official_last_price_and_utc(self):
        tick = BinanceStreamClient._parse_tick({"e": "24hrTicker", "s": "BTCUSDT", "c": "123.45", "Q": "0.1", "E": 1_700_000_000_000})
        self.assertEqual(tick.price, Decimal("123.45"))
        self.assertEqual(tick.timestamp.tzinfo, UTC)

    def test_trade_event_parses_price_quantity_and_utc(self):
        tick = BinanceStreamClient._parse_tick({"e": "trade", "s": "BTCUSDT", "p": "123.45", "q": "0.5", "T": 1_700_000_000_000})
        self.assertEqual(tick.price, Decimal("123.45"))
        self.assertEqual(tick.quantity, Decimal("0.5"))
        self.assertEqual(tick.timestamp.tzinfo, UTC)


class TickStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_and_out_of_order_updates_are_rejected(self):
        store = TickStore()
        newer = Tick(symbol="BTCUSDT", price="2", quantity="1", timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        older = Tick(symbol="BTCUSDT", price="1", quantity="1", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        self.assertTrue(await store.update(newer))
        self.assertFalse(await store.update(older))
        self.assertFalse(await store.update(newer))
        self.assertEqual((await store.get("BTCUSDT")).price, Decimal("2"))


class CandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ohlc_boundary_late_tick_and_symbol_separation(self):
        emitted = []
        service = CandleService(10, 60, emitted.append)
        base = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
        for symbol, price, second in (("BTCUSDT", "100", 5), ("BTCUSDT", "105", 20), ("BTCUSDT", "98", 40), ("ETHUSDT", "20", 45)):
            await service.process_tick(Tick(symbol=symbol, price=price, quantity="1", timestamp=base.replace(second=second)))
        await service.process_tick(Tick(symbol="BTCUSDT", price="102", quantity="1", timestamp=base.replace(minute=1, second=1)))
        history = await service.history("BTCUSDT", 10)
        self.assertEqual((history[0].open, history[0].high, history[0].low, history[0].close), (Decimal("100"), Decimal("105"), Decimal("98"), Decimal("98")))
        await service.process_tick(Tick(symbol="BTCUSDT", price="1", quantity="1", timestamp=base))
        self.assertEqual((await service.current("BTCUSDT")).close, Decimal("102"))
        self.assertEqual((await service.current("ETHUSDT")).close, Decimal("20"))

    async def test_late_tick_after_boundary_finalization_does_not_duplicate(self):
        emitted = []
        service = CandleService(10, 60, emitted.append)
        base = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
        await service.process_tick(Tick(symbol="BTCUSDT", price="100", quantity="1", timestamp=base))
        await service._finalize_due(datetime(2026, 1, 1, 12, 1, tzinfo=UTC))
        await service.process_tick(Tick(symbol="BTCUSDT", price="101", quantity="1", timestamp=base.replace(second=40)))

        self.assertIsNone(await service.current("BTCUSDT"))
        history = await service.history("BTCUSDT", 10)
        self.assertEqual([item.interval_start for item in history], [base.replace(second=0)])

    async def test_skipped_minute_is_filled_with_a_flat_candle(self):
        emitted = []
        service = CandleService(10, 60, emitted.append)
        base = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC).replace(second=5)
        await service.process_tick(Tick(symbol="BTCUSDT", price="100", quantity="1", timestamp=base))
        await service.process_tick(Tick(symbol="BTCUSDT", price="110", quantity="1", timestamp=base.replace(minute=2)))

        history = await service.history("BTCUSDT", 10)
        self.assertEqual([item.interval_start for item in history], [base.replace(second=0), base.replace(minute=1, second=0)])
        filler = history[1]
        self.assertTrue(filler.is_final)
        self.assertEqual(filler.tick_count, 0)
        self.assertEqual((filler.open, filler.high, filler.low, filler.close), (Decimal("100"),) * 4)
        self.assertEqual((await service.current("BTCUSDT")).interval_start, base.replace(minute=2, second=0))
        self.assertIn(filler, emitted)

    async def test_boundary_loop_emits_a_finalized_candle(self):
        loop = asyncio.get_running_loop()
        finalized = loop.create_future()

        def on_candle(value: Candle) -> None:
            if value.is_final and not finalized.done():
                finalized.set_result(value)

        service = CandleService(10, 1, on_candle)
        await service.process_tick(
            Tick(symbol="BTCUSDT", price="100", quantity="1", timestamp=datetime.now(UTC))
        )
        await service.start()
        try:
            candle = await asyncio.wait_for(finalized, timeout=3)
        finally:
            await service.stop()
        self.assertTrue(candle.is_final)
        self.assertEqual(candle.symbol, "BTCUSDT")


class CandleModelTests(unittest.TestCase):
    def build(self, start: datetime, span: timedelta) -> Candle:
        value = Decimal("100")
        return Candle(symbol="BTCUSDT", interval_start=start, interval_end=start + span, open=value, high=value, low=value, close=value, tick_count=0, is_final=True)

    def test_aligned_non_minute_interval_is_accepted_when_ohlc_is_valid(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        value = Decimal("100")
        candle = Candle(
            symbol="BTCUSDT", interval_start=start, interval_end=start + timedelta(minutes=5),
            open=value, high=Decimal("101"), low=Decimal("99"), close=value, tick_count=0, is_final=True,
        )
        self.assertEqual(candle.interval_end - candle.interval_start, timedelta(minutes=5))
        # A high below the open/close must be rejected as inconsistent OHLC.
        with self.assertRaises(ValueError):
            Candle(
                symbol="BTCUSDT", interval_start=start, interval_end=start + timedelta(minutes=5),
                open=value, high=Decimal("99"), low=Decimal("99"), close=value, tick_count=0, is_final=True,
            )

    def test_misaligned_interval_start_is_rejected(self):
        start = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
        with self.assertRaises(ValueError):
            self.build(start, timedelta(minutes=5))

    def test_fractional_interval_is_rejected(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        with self.assertRaises(ValueError):
            self.build(start, timedelta(seconds=90.5))
