import asyncio
import unittest
from decimal import Decimal

from services.pretrade_risk import PreTradeRiskEngine, PreTradeRiskError
from services.reconciliation_service import ReconciliationService


class ExecutionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_orders_cannot_double_spend_free_balance(self):
        engine = PreTradeRiskEngine(Decimal("80"))
        rules = {"_quoteAsset": "USDT"}

        async def account():
            await asyncio.sleep(0)
            return {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}

        async def price(_symbol):
            return Decimal("1")

        order = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "60"}
        results = await asyncio.gather(
            engine.reserve(7, order, rules, account, price),
            engine.reserve(7, order, rules, account, price),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        self.assertEqual(sum(isinstance(item, PreTradeRiskError) for item in results), 1)

    async def test_unknown_order_is_reconciled_without_resubmission(self):
        service = ReconciliationService(attempts=3)
        calls = []

        async def query(symbol, client_id):
            calls.append((symbol, client_id))
            if len(calls) == 1:
                raise RuntimeError("temporarily unavailable")
            return {"symbol": symbol, "clientOrderId": client_id, "orderId": 99, "status": "FILLED"}

        result = await service.reconcile_unknown(
            {"symbol": "BTCUSDT", "newClientOrderId": "cts-test-1"}, query
        )
        self.assertEqual(result["orderId"], 99)
        self.assertEqual(len(calls), 2)

    async def test_hard_notional_limit_rejects_fat_finger_order(self):
        engine = PreTradeRiskEngine(Decimal("80"), Decimal("50"))

        async def account():
            return {"balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]}

        async def price(_symbol):
            return Decimal("1")

        with self.assertRaisesRegex(PreTradeRiskError, "exceeds maximum"):
            await engine.reserve(
                7,
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "51"},
                {"_quoteAsset": "USDT"},
                account,
                price,
            )
