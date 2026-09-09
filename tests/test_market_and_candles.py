import unittest
from datetime import UTC, datetime
from decimal import Decimal

from models import Tick
from services.candle_service import CandleService
from services.market_data import BinanceStreamClient
from services.tick_store import TickStore


class MarketParsingTests(unittest.TestCase):
    def test_ticker_uses_official_last_price_and_utc(self):
        tick = BinanceStreamClient._parse_tick({"e": "24hrTicker", "s": "BTCUSDT", "c": "123.45", "Q": "0.1", "E": 1_700_000_000_000})
        self.assertEqual(tick.price, Decimal("123.45"))
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
