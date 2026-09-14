"""Execution-safety paths that the existing suite left unexercised:

- the consecutive-failure circuit breaker,
- reconciliation of an order whose submission outcome is unknown,
- the per-user kill switch,
- quote-balance-aware buy sizing.
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from config import get_settings
from services.order_service import BinanceOrderError, OrderService, _UserSession, current_user_id


class OrderSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = get_settings().model_copy(
            update={
                "state_database_path": str(Path(self.temp.name) / "state.db"),
                "max_consecutive_order_failures": 2,
            }
        )
        self.service = OrderService(settings)
        self.service._session_execution_enabled = True
        self.service._symbol_rules["BTCUSDT"] = {
            "LOT_SIZE": {"minQty": "0.00001", "maxQty": "100", "stepSize": "0.00001"},
            "PRICE_FILTER": {"minPrice": "0.01", "tickSize": "0.01"},
            "MIN_NOTIONAL": {"minNotional": "5"},
            "_baseAsset": "BTC",
            "_quoteAsset": "USDT",
        }
        self.service._exchange_info_loaded_at["BTCUSDT"] = 10**12

        async def no_op(*args, **kwargs):
            return None

        self.service._enforce_min_notional = no_op

    def tearDown(self):
        self.temp.cleanup()

    async def test_consecutive_exchange_failures_open_the_circuit_breaker(self):
        async def failing_request(method, path, params):
            raise BinanceOrderError("exchange unavailable", 502)

        async def reserve(*args, **kwargs):
            return None

        self.service._signed_request = failing_request
        self.service._risk.reserve = reserve
        order = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"}

        for _ in range(2):
            with self.assertRaises(BinanceOrderError) as failure:
                await self.service.place_order(order)
            self.assertNotEqual(failure.exception.status_code, 503)

        with self.assertRaises(BinanceOrderError) as open_circuit:
            await self.service.place_order(order)
        self.assertEqual(open_circuit.exception.status_code, 503)
        self.assertIn("circuit breaker", str(open_circuit.exception))

    async def test_uncertain_order_is_reconciled_and_reservation_acknowledged(self):
        reconciled = {"orderId": 55, "status": "FILLED", "clientOrderId": "cts-x"}
        acknowledged = {}

        async def uncertain_request(method, path, params):
            raise BinanceOrderError("timeout", uncertain=True)

        async def reserve(*args, **kwargs):
            return object()

        async def mark_uncertain(reservation):
            acknowledged["uncertain"] = True

        async def acknowledge(reservation, status):
            acknowledged["status"] = status

        async def reconcile(order, query):
            return reconciled

        self.service._signed_request = uncertain_request
        self.service._risk.reserve = reserve
        self.service._risk.mark_uncertain = mark_uncertain
        self.service._risk.acknowledge = acknowledge
        self.service._reconciliation.reconcile_unknown = reconcile

        result = await self.service.place_order(
            {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"}
        )
        self.assertEqual(result, reconciled)
        self.assertTrue(acknowledged.get("uncertain"))
        self.assertEqual(acknowledged.get("status"), "FILLED")

    async def test_unresolved_uncertain_order_keeps_the_reservation(self):
        async def uncertain_request(method, path, params):
            raise BinanceOrderError("timeout", uncertain=True)

        async def reserve(*args, **kwargs):
            return object()

        async def no_op(*args, **kwargs):
            return None

        async def unresolved(order, query):
            return None

        self.service._signed_request = uncertain_request
        self.service._risk.reserve = reserve
        self.service._risk.mark_uncertain = no_op
        self.service._reconciliation.reconcile_unknown = unresolved

        with self.assertRaises(BinanceOrderError) as context:
            await self.service.place_order(
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"}
            )
        self.assertTrue(context.exception.uncertain)
        self.assertEqual(context.exception.status_code, 503)

    async def test_per_user_kill_switch_follows_the_session(self):
        session = _UserSession(SecretStr("k"), SecretStr("s"))
        session.verified = True
        self.service._user_sessions[1] = session
        token = current_user_id.set(1)
        try:
            self.assertFalse(self.service.execution_enabled)
            session.enabled = True
            self.assertTrue(self.service.execution_enabled)
        finally:
            current_user_id.reset(token)

    async def test_buyable_quantity_caps_to_free_quote_balance_and_step(self):
        self.service._symbol_rules["BTCUSDT"]["MARKET_LOT_SIZE"] = {
            "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"
        }

        async def account():
            return {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}

        async def market_price(_symbol):
            return Decimal("40000")

        self.service.account = account
        self.service._market_price = market_price
        quantity = await self.service._buyable_quantity("BTCUSDT", Decimal("1"))
        self.assertEqual(quantity, Decimal("0.0025"))
