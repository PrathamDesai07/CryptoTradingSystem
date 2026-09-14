"""Exercise the real signed-request / HTTP client path against a mock transport.

Unlike the other order tests, nothing here monkey-patches ``_signed_request`` or
``place_order``: an ``httpx.MockTransport`` is injected so the actual query
construction, HMAC signing and error mapping run for real.
"""

import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import httpx
from pydantic import SecretStr

from config import get_settings
from services.order_service import BinanceOrderError, OrderService


class BinanceHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = get_settings().model_copy(
            update={"state_database_path": str(Path(self.temp.name) / "state.db")}
        )
        self.service = OrderService(settings)
        self.service._runtime_api_key = SecretStr("api-key")
        self.service._runtime_api_secret = SecretStr("api-secret")

    def tearDown(self):
        self.temp.cleanup()

    async def use_transport(self, handler) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.service._http_client = client
        self.addAsyncCleanup(client.aclose)

    async def test_signed_request_uses_hmac_sha256_and_required_fields(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"ok": True})

        await self.use_transport(handler)
        await self.service._signed_request("GET", "/api/v3/account", {"omitZeroBalances": "true"})

        request = captured["request"]
        self.assertEqual(request.url.path, "/api/v3/account")
        self.assertEqual(request.headers["X-MBX-APIKEY"], "api-key")

        fields = dict(parse_qsl(request.url.query.decode()))
        signature = fields.pop("signature")
        expected = hmac.new(b"api-secret", urlencode(fields).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertIn("timestamp", fields)
        self.assertEqual(
            fields["recvWindow"], str(self.service._settings.order_recv_window_milliseconds)
        )

    async def test_public_request_sends_no_api_key(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"symbols": []})

        await self.use_transport(handler)
        await self.service._public_request("GET", "/api/v3/exchangeInfo", {"symbol": "BTCUSDT"})
        self.assertNotIn("X-MBX-APIKEY", captured["request"].headers)

    async def test_http_error_body_becomes_credential_free_order_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"code": -1013, "msg": "Filter failure: NOTIONAL"})

        await self.use_transport(handler)
        with self.assertRaises(BinanceOrderError) as context:
            await self.service._signed_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})
        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.code, -1013)
        self.assertIn("Filter failure", str(context.exception))
        self.assertFalse(context.exception.uncertain)

    async def test_timeout_is_reported_as_uncertain(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        await self.use_transport(handler)
        with self.assertRaises(BinanceOrderError) as context:
            await self.service._signed_request("GET", "/api/v3/account", {})
        self.assertTrue(context.exception.uncertain)

    async def test_transport_error_is_reported_as_uncertain(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection reset")

        await self.use_transport(handler)
        with self.assertRaises(BinanceOrderError) as context:
            await self.service._signed_request("GET", "/api/v3/account", {})
        self.assertTrue(context.exception.uncertain)
