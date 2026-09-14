import unittest
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from models import Candle, Signal, SignalAction, StrategyVariant
from config import get_settings
from services.order_service import OrderService
from services.strategy_service import StrategyService


def seed_user(repository, user_id: int) -> None:
    """Create a users row so per-user foreign keys accept this test id."""
    with closing(repository._connect()) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO users(id, username, display_name, password_hash, created_at)"
            " VALUES(?,?,?,?,?)",
            (user_id, f"test-user-{user_id}", f"Test User {user_id}", "hash", "2026-01-01T00:00:00Z"),
        )


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

    async def test_exact_sma_and_ema_values_after_warmup(self):
        service = StrategyService(2, 3, 20, 20)
        for index, close in enumerate(("10", "9", "8", "12")):
            await service.process_candle(candle(index, close))
        points = await service.indicators("BTCUSDT", 10)
        self.assertIsNone(points[0].sma)
        self.assertEqual(points[2].sma, Decimal("8.5"))
        self.assertEqual(points[2].ema, Decimal("8.75"))
        self.assertEqual(points[3].sma, Decimal("10"))
        self.assertEqual(points[3].ema, Decimal("10.375"))
        self.assertEqual(await service.relation("BTCUSDT"), -1)

    async def test_seed_rebuilds_state_without_emitting_signals(self):
        emitted = []
        service = StrategyService(2, 3, 20, 20, emitted.append)
        history = [candle(index, close) for index, close in enumerate(("10", "9", "8", "12", "13"))]
        await service.seed("BTCUSDT", history)
        self.assertEqual(emitted, [])
        self.assertIsNotNone(await service.relation("BTCUSDT"))
        self.assertEqual(len(await service.indicators("BTCUSDT", 10)), len(history))

    async def test_signal_history_and_latest_signals_are_queryable(self):
        service = StrategyService(2, 3, 20, 20)
        for index, close in enumerate(("10", "9", "8", "12", "13", "7", "1")):
            await service.process_candle(candle(index, close))
        history = await service.signals("BTCUSDT", 10)
        self.assertTrue(history)
        self.assertEqual({signal.symbol for signal in history}, {"BTCUSDT"})
        latest = await service.latest_signals("BTCUSDT")
        self.assertEqual(set(latest), {StrategyVariant.A, StrategyVariant.B})

    async def test_remove_symbol_clears_state(self):
        service = StrategyService(2, 3, 20, 20)
        for index, close in enumerate(("10", "9", "8", "12", "13")):
            await service.process_candle(candle(index, close))
        await service.remove_symbol("BTCUSDT")
        self.assertEqual(await service.indicators("BTCUSDT", 10), ())
        self.assertIsNone(await service.relation("BTCUSDT"))
        self.assertEqual(await service.latest_signals("BTCUSDT"), {})

    async def test_finalized_crossover_places_three_protected_orders_per_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = get_settings().model_copy(update={"state_database_path": str(Path(directory) / "state.db")})
            orders = OrderService(settings)
            seed_user(orders._repository, 7)
            orders._user_sessions[7] = SimpleNamespace(verified=True, enabled=True)
            submitted = []

            async def place_order(params, test=False):
                submitted.append(params)
                return {"executedQty": params["quantity"], "cummulativeQuoteQty": "8"}

            orders.place_order = place_order

            async def execute(signal):
                await orders.process_signal_for_user(7, signal)

            strategy = StrategyService(2, 3, 20, 20, execute)
            for index, close in enumerate(("10", "9", "8", "12", "13")):
                await strategy.process_candle(candle(index, close))

            self.assertEqual(len(submitted), 6)
            self.assertEqual({item["type"] for item in submitted}, {"MARKET", "STOP_LOSS", "TAKE_PROFIT"})
            self.assertEqual({item["_audit"]["strategy_variant"] for item in submitted}, {"A", "B"})
            self.assertTrue(all(item["_audit"]["source"] == "strategy" for item in submitted))

    async def test_variants_differ_only_in_stop_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = get_settings().model_copy(update={"state_database_path": str(Path(directory) / "state.db")})
            orders = OrderService(settings)
            seed_user(orders._repository, 7)
            orders._user_sessions[7] = SimpleNamespace(verified=True, enabled=True)

            async def place_order(params, test=False):
                return {"executedQty": params["quantity"], "cummulativeQuoteQty": "8"}

            orders.place_order = place_order
            for variant in (StrategyVariant.A, StrategyVariant.B):
                await orders.process_signal_for_user(7, Signal(
                    symbol="BTCUSDT", variant=variant, action=SignalAction.BUY,
                    price=Decimal("80000"), fast_value=Decimal("80100"), slow_value=Decimal("80000"),
                    reason="test crossover", timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                ))

            position_a = orders._positions[(7, "BTCUSDT", StrategyVariant.A)]
            position_b = orders._positions[(7, "BTCUSDT", StrategyVariant.B)]
            self.assertEqual(position_a.entry_price, position_b.entry_price)
            self.assertEqual(position_a.stop_loss_price, position_a.entry_price * Decimal("0.85"))
            self.assertEqual(position_b.stop_loss_price, position_b.entry_price * Decimal("0.9"))
            self.assertNotEqual(position_a.stop_loss_price, position_b.stop_loss_price)
            self.assertEqual(position_a.take_profit_price, position_b.take_profit_price)
            self.assertEqual(position_a.take_profit_price, position_a.entry_price * Decimal("1.05"))
