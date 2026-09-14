"""Market-data stream resilience: message handling, reconnection and stale timeouts.

These paths (``_handle_message``, ``_run`` backoff, the stale-read timeout) were
previously untested even though graceful reconnect/stale handling is an explicit
assignment requirement.
"""

import asyncio
import json
import unittest
from unittest import mock

from config import get_settings
from models import PartialOrderBook
from services.market_data import BinanceStreamClient, ServerShutdownError
from services.order_book_store import OrderBookStore
from services.tick_store import TickStore


def make_settings(**overrides):
    return get_settings().model_copy(update=overrides)


class HandleMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = make_settings(trading_symbols="BTCUSDT,ETHUSDT")
        self.tick_store = TickStore()
        self.order_book_store = OrderBookStore()
        self.client = BinanceStreamClient(
            self.settings, self.tick_store, order_book_store=self.order_book_store
        )

    async def test_invalid_json_is_ignored(self):
        await self.client._handle_message("{not valid json")
        self.assertEqual(await self.tick_store.snapshot(), {})

    async def test_non_dict_payload_is_ignored(self):
        await self.client._handle_message(json.dumps([1, 2, 3]))
        self.assertEqual(await self.tick_store.snapshot(), {})

    async def test_server_shutdown_raises(self):
        payload = {"stream": "!serverShutdown", "data": {"e": "serverShutdown"}}
        with self.assertRaises(ServerShutdownError):
            await self.client._handle_message(json.dumps(payload))

    async def test_subscription_response_is_ignored(self):
        await self.client._handle_message(json.dumps({"result": None, "id": 1}))
        self.assertEqual(await self.tick_store.snapshot(), {})

    async def test_valid_trade_updates_the_tick_store(self):
        payload = {
            "stream": "btcusdt@trade",
            "data": {"e": "trade", "s": "BTCUSDT", "p": "100.5", "q": "2", "T": 1_700_000_000_000},
        }
        await self.client._handle_message(json.dumps(payload))
        tick = await self.tick_store.get("BTCUSDT")
        self.assertIsNotNone(tick)
        self.assertEqual(str(tick.price), "100.5")

    async def test_malformed_trade_is_rejected_without_raising(self):
        payload = {"stream": "btcusdt@trade", "data": {"e": "trade", "s": "BTCUSDT"}}
        await self.client._handle_message(json.dumps(payload))
        self.assertIsNone(await self.tick_store.get("BTCUSDT"))

    async def test_unknown_event_is_ignored(self):
        payload = {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT"}}
        await self.client._handle_message(json.dumps(payload))
        self.assertIsNone(await self.tick_store.get("BTCUSDT"))

    async def test_depth_event_updates_the_order_book_store(self):
        payload = {
            "stream": "btcusdt@depth10@100ms",
            "data": {
                "lastUpdateId": 7,
                "bids": [["100.00", "1.5"], ["99.50", "2.0"]],
                "asks": [["100.50", "1.1"], ["101.00", "3.0"]],
            },
        }
        await self.client._handle_message(json.dumps(payload))
        book = await self.order_book_store.get("BTCUSDT")
        self.assertIsInstance(book, PartialOrderBook)
        self.assertEqual(book.last_update_id, 7)
        self.assertEqual(str(book.bids[0].price), "100.00")
        self.assertEqual(str(book.asks[0].price), "100.50")


class StreamLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_retries_with_backoff_then_stops(self):
        settings = make_settings(
            websocket_reconnect_initial_seconds=0.001,
            websocket_reconnect_max_seconds=0.002,
            websocket_reconnect_jitter_ratio=0.0,
        )
        client = BinanceStreamClient(settings, TickStore())
        attempts = {"count": 0}

        async def connect_once():
            attempts["count"] += 1
            if attempts["count"] >= 3:
                client._stop_event.set()
            raise OSError("stream dropped")

        client._connect_once = connect_once
        await asyncio.wait_for(client._run(), timeout=5)
        self.assertEqual(attempts["count"], 3)

    async def test_connect_once_raises_when_the_stream_goes_stale(self):
        settings = make_settings(
            websocket_stale_timeout_seconds=0.01, trading_symbols="BTCUSDT"
        )
        client = BinanceStreamClient(settings, TickStore())

        class FakeSocket:
            async def recv(self):
                await asyncio.sleep(30)

            async def send(self, _data):
                return None

            async def close(self):
                return None

        class FakeConnect:
            async def __aenter__(self):
                return FakeSocket()

            async def __aexit__(self, *_exc):
                return False

        with mock.patch("services.market_data.connect", lambda *_a, **_k: FakeConnect()):
            with self.assertRaises(TimeoutError):
                await client._connect_once()
