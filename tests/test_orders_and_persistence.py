import tempfile
import unittest
from types import SimpleNamespace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from config import get_settings
from models import Position, PositionStatus, Signal, SignalAction, StrategyVariant, Tick
from services.order_service import BinanceOrderError, OrderService, current_user_id
from services.db_handler import DatabaseHandler, hash_password
from services.pretrade_risk import PreTradeRiskError
from services.state_repository import StateRepository


class OrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = get_settings().model_copy(update={"state_database_path": str(Path(self.temp.name) / "state.db")})
        self.service = OrderService(self.settings)
        self.service._symbol_rules["BTCUSDT"] = {
            "LOT_SIZE": {"minQty": "0.00001", "maxQty": "100", "stepSize": "0.00001"},
            "PRICE_FILTER": {"minPrice": "0.01", "tickSize": "0.01"},
            "MIN_NOTIONAL": {"minNotional": "5"},
            "_baseAsset": "BTC", "_quoteAsset": "USDT",
        }
        self.service._exchange_info_loaded_at["BTCUSDT"] = 10**12

    def tearDown(self):
        self.temp.cleanup()

    async def test_all_single_types_validate_and_filters_round_down(self):
        cases = [
            {"type": "MARKET", "quoteOrderQty": "10"},
            {"type": "LIMIT", "quantity": "0.001239", "price": "80000.129", "timeInForce": "GTC"},
            {"type": "LIMIT_MAKER", "quantity": "0.001", "price": "80000"},
            {"type": "STOP_LOSS", "quantity": "0.001", "stopPrice": "79000"},
            {"type": "STOP_LOSS_LIMIT", "quantity": "0.001", "price": "78900", "stopPrice": "79000", "timeInForce": "GTC"},
            {"type": "TAKE_PROFIT", "quantity": "0.001", "stopPrice": "81000"},
            {"type": "TAKE_PROFIT_LIMIT", "quantity": "0.001", "price": "81100", "stopPrice": "81000", "timeInForce": "GTC"},
        ]
        normalized = [await self.service._normalize_order({"symbol": "BTCUSDT", "side": "BUY", **case}) for case in cases]
        self.assertEqual(len(normalized), 7)
        self.assertEqual(normalized[1]["quantity"], "0.00123")
        self.assertEqual(normalized[1]["price"], "80000.12")

    async def test_kill_switch_and_missing_stop_are_rejected(self):
        with self.assertRaises(BinanceOrderError):
            await self.service.place_order({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "10"})
        with self.assertRaises(BinanceOrderError):
            await self.service._normalize_order({"symbol": "BTCUSDT", "side": "BUY", "type": "STOP_LOSS", "quantity": "0.001"})

    async def test_strategy_quantity_updates_and_sizes_market_entry(self):
        self.service._session_execution_enabled = True
        submitted = []

        async def place_order(params, test=False):
            submitted.append(params)
            return {
                "executedQty": params["quantity"],
                "cummulativeQuoteQty": "8",
            }

        self.service.place_order = place_order
        await self.service.set_strategy_order_quantity(Decimal("0.0002"))
        signal = Signal(
            symbol="BTCUSDT",
            variant=StrategyVariant.A,
            action=SignalAction.BUY,
            price=Decimal("40000"),
            fast_value=Decimal("40100"),
            slow_value=Decimal("40000"),
            reason="test crossover",
            timestamp=datetime.now(UTC),
        )
        await self.service.process_signal(signal)

        self.assertEqual(submitted[0]["type"], "MARKET")
        self.assertEqual(submitted[0]["quantity"], "0.0002")
        position = (await self.service.positions("BTCUSDT"))[0]
        self.assertEqual(position.quantity, Decimal("0.0002"))
        self.assertEqual(position.stop_loss_price, Decimal("36000"))
        self.assertEqual(position.take_profit_price, Decimal("42000"))

    async def test_background_strategy_signal_uses_active_user_context(self):
        self.service._user_sessions[7] = SimpleNamespace(verified=True, enabled=True)
        observed_user_ids = []

        async def place_order(params, test=False):
            observed_user_ids.append(current_user_id.get())
            return {"executedQty": params["quantity"], "cummulativeQuoteQty": "8"}

        self.service.place_order = place_order
        signal = Signal(
            symbol="BTCUSDT", variant=StrategyVariant.A, action=SignalAction.BUY,
            price=Decimal("40000"), fast_value=Decimal("40100"), slow_value=Decimal("40000"),
            reason="test crossover", timestamp=datetime.now(UTC),
        )
        await self.service.process_signal_for_user(7, signal)
        self.assertEqual(observed_user_ids, [7])
        self.assertIn((7, "BTCUSDT", StrategyVariant.A), self.service._positions)

    async def test_two_users_can_hold_same_symbol_and_variant_independently(self):
        self.service._user_sessions[7] = SimpleNamespace(verified=True, enabled=True)
        self.service._user_sessions[8] = SimpleNamespace(verified=True, enabled=True)

        async def place_order(params, test=False):
            return {"executedQty": params["quantity"], "cummulativeQuoteQty": "8"}

        self.service.place_order = place_order
        signal = Signal(
            symbol="BTCUSDT", variant=StrategyVariant.A, action=SignalAction.BUY,
            price=Decimal("40000"), fast_value=Decimal("40100"), slow_value=Decimal("40000"),
            reason="test crossover", timestamp=datetime.now(UTC),
        )
        await self.service.process_signal_for_user(7, signal)
        await self.service.process_signal_for_user(8, signal)
        self.assertIn((7, "BTCUSDT", StrategyVariant.A), self.service._positions)
        self.assertIn((8, "BTCUSDT", StrategyVariant.A), self.service._positions)

    async def test_strategy_audit_metadata_is_persisted_for_user(self):
        captured = []
        self.service._repository = SimpleNamespace(
            log_user_order=lambda user_id, record, source: captured.append((user_id, record, source))
        )
        token = current_user_id.set(7)
        try:
            await self.service._record(
                {"orderId": 42, "status": "FILLED", "executedQty": "0.001"},
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET"},
                {"source": "strategy", "strategy_variant": "A", "signal_reason": "SMA crossed above EMA"},
            )
        finally:
            current_user_id.reset(token)
        self.assertEqual(captured[0][0], 7)
        self.assertEqual(captured[0][1]["strategy_variant"], "A")
        self.assertEqual(captured[0][2], "strategy")

    async def test_strategy_quantity_must_be_positive(self):
        with self.assertRaises(BinanceOrderError):
            await self.service.set_strategy_order_quantity(Decimal("0"))

    async def test_square_off_quantity_is_capped_to_free_balance_and_market_step(self):
        self.service._symbol_rules["BTCUSDT"]["MARKET_LOT_SIZE"] = {
            "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"
        }

        async def account():
            return {"balances": [{"asset": "BTC", "free": "0.00995", "locked": "0"}]}

        self.service.account = account
        quantity = await self.service._sellable_quantity("BTCUSDT", Decimal("0.01"))
        self.assertEqual(quantity, Decimal("0.0099"))

    async def test_balance_utilization_guard_rejects_oversized_buy_but_allows_sell(self):
        async def account():
            return {"balances": [
                {"asset": "USDT", "free": "100", "locked": "0"},
                {"asset": "BTC", "free": "0.02", "locked": "0"},
            ]}

        async def market_price(_symbol):
            return Decimal("40000")

        self.service.account = account
        self.service._market_price = market_price
        with self.assertRaisesRegex(BinanceOrderError, "risk guard rejected"):
            await self.service._check_balance_utilization({
                "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "81"
            })
        await self.service._check_balance_utilization({
            "symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.02"
        })

    async def test_stale_market_data_rejects_buy_but_not_protective_sell(self):
        stale = Tick(
            symbol="BTCUSDT", price="40000", quantity="1",
            timestamp=datetime(2020, 1, 1, tzinfo=UTC),
        )

        async def load_tick(_symbol):
            return stale

        self.service._market_tick_loader = load_tick
        with self.assertRaisesRegex(PreTradeRiskError, "market data is stale"):
            await self.service._ensure_market_fresh({"symbol": "BTCUSDT", "side": "BUY"})
        await self.service._ensure_market_fresh({"symbol": "BTCUSDT", "side": "SELL"})

    async def test_fifo_pnl_includes_base_and_quote_fees(self):
        trades = [
            {"id": 1, "orderId": 10, "qty": "1", "quoteQty": "100", "commission": "0.01", "commissionAsset": "BTC", "time": 1, "isBuyer": True},
            {"id": 2, "orderId": 11, "qty": "0.4", "quoteQty": "48", "commission": "1", "commissionAsset": "USDT", "time": 2, "isBuyer": False},
        ]
        async def signed(method, path, params):
            return trades if "fromId" not in params else []
        self.service._signed_request = signed
        result = await self.service.pnl("BTCUSDT", Decimal("110"))
        self.assertEqual(result["open_quantity"], Decimal("0.59"))
        self.assertEqual(result["realized_pnl"], Decimal("0"))
        self.assertGreater(result["unrealized_pnl"], 0)

    async def test_fifo_pnl_tracks_sell_order_until_buy_square_off(self):
        trades = [
            {"id": 1, "orderId": 21, "qty": "0.01", "quoteQty": "400", "commission": "0", "commissionAsset": "USDT", "time": 1, "isBuyer": False},
        ]

        async def all_trades(_symbol):
            return trades

        self.service._all_trades = all_trades
        result = await self.service.pnl("BTCUSDT", Decimal("39000"))
        order = next(item for item in result["orders"] if item["order_id"] == 21)
        self.assertEqual(order["remaining_quantity"], Decimal("0.01"))
        self.assertEqual(result["open_quantity"], Decimal("-0.01"))

        trades.append({"id": 2, "orderId": 22, "qty": "0.01", "quoteQty": "390", "commission": "0", "commissionAsset": "USDT", "time": 2, "isBuyer": True})
        result = await self.service.pnl("BTCUSDT", Decimal("39000"))
        self.assertEqual(result["open_quantity"], Decimal("0"))
        self.assertEqual(next(item for item in result["orders"] if item["order_id"] == 21)["remaining_quantity"], Decimal("0.01"))
        self.assertEqual(next(item for item in result["orders"] if item["order_id"] == 22)["remaining_quantity"], Decimal("0.01"))

    async def test_order_list_types_use_distinct_official_paths(self):
        self.service._session_execution_enabled = True
        calls = []
        async def signed(method, path, params):
            calls.append((method, path, params))
            return {"orderReports": []}
        self.service._signed_request = signed
        for kind in ("OCO", "OTO", "OTOCO", "OPO", "OPOCO"):
            await self.service.place_order_list(kind, {"symbol": "BTCUSDT"})
        self.assertEqual([path for _, path, _ in calls], list(self.service.ORDER_LIST_PATHS.values()))

    async def test_complete_trade_history_is_paginated(self):
        pages = [[{"id": index} for index in range(1000)], [{"id": 1000}]]
        async def signed(method, path, params):
            return pages.pop(0)
        self.service._signed_request = signed
        trades = await self.service._all_trades("BTCUSDT")
        self.assertEqual(len(trades), 1001)

    def test_sqlite_restores_orders_and_positions(self):
        repository = StateRepository(str(Path(self.temp.name) / "restore.db"), 10)
        repository.save_order({"recordedAt": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "orderId": 1, "status": "FILLED"})
        position = Position(symbol="BTCUSDT", variant=StrategyVariant.A, status=PositionStatus.OPEN, quantity="0.01", entry_price="100", current_price="101", current_pnl="0.01", stop_loss_price="90", take_profit_price="105", opened_at=datetime.now(UTC))
        repository.save_position(position)
        self.assertEqual(repository.load_orders()[0]["orderId"], 1)
        self.assertEqual(repository.load_positions()[0].variant, StrategyVariant.A)

    def test_user_position_ownership_survives_restart(self):
        path = str(Path(self.temp.name) / "users.db")
        repository = DatabaseHandler(path, 10)
        user_id = repository.create_user("owner", "Owner", hash_password("secret1"))
        position = Position(symbol="BTCUSDT", variant=StrategyVariant.A, status=PositionStatus.OPEN, quantity="0.01", entry_price="100", current_price="101", current_pnl="0.01", stop_loss_price="90", take_profit_price="105", opened_at=datetime.now(UTC))
        repository.save_user_position(user_id, position)
        restored = repository.load_user_positions()
        self.assertEqual(restored[0][0], user_id)
        self.assertEqual(restored[0][1].symbol, "BTCUSDT")

    def test_only_one_process_can_hold_sqlite_execution_lease(self):
        path = str(Path(self.temp.name) / "lease.db")
        first = DatabaseHandler(path, 10)
        second = DatabaseHandler(path, 10)
        self.assertTrue(first.acquire_execution_lease())
        self.assertFalse(second.acquire_execution_lease())
        first.release_execution_lease()
        self.assertTrue(second.acquire_execution_lease())
        second.release_execution_lease()
