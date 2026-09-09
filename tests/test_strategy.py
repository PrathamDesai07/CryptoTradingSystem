import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from models import Candle
from services.strategy_service import StrategyService


def candle(index: int, close: str) -> Candle:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    value = Decimal(close)
    return Candle(symbol="BTCUSDT", interval_start=start, interval_end=start + timedelta(minutes=1), open=value, high=value, low=value, close=value, tick_count=1, is_final=True)


class StrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_sma_ema_crossovers_emit_once_for_each_variant(self):
        service = StrategyService(2, 3, 20, 20)
        actions = []
        for index, close in enumerate(("10", "9", "8", "12", "13", "7", "1")):
            actions.extend(signal.action.value for signal in await service.process_candle(candle(index, close)))
        self.assertEqual(actions.count("BUY"), 2)
        self.assertEqual(actions.count("EXIT"), 2)

    async def test_non_final_candle_is_ignored(self):
        service = StrategyService(2, 3, 20, 20)
        item = candle(0, "10").model_copy(update={"is_final": False})
        self.assertEqual(await service.process_candle(item), ())
