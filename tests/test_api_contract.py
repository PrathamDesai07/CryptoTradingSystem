import unittest

from pydantic import ValidationError

from api.routes import SpotOrderRequest


class ApiContractTests(unittest.TestCase):
    def test_order_payload_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            SpotOrderRequest(symbol="BTCUSDT", side="BUY", type="MARKET", unknown="secret")

    def test_order_payload_normalizes_symbol_and_enums(self):
        payload = SpotOrderRequest(symbol="btcusdt", side="buy", type="limit", timeInForce="gtc", quantity="1", price="1")
        self.assertEqual((payload.symbol, payload.side, payload.type, payload.timeInForce), ("BTCUSDT", "BUY", "LIMIT", "GTC"))
