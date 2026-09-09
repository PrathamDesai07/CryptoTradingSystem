"""Fan live ticks out to connected frontend WebSocket clients."""

import asyncio
import logging

from fastapi import WebSocket

from models import Tick

logger = logging.getLogger(__name__)


class TickBroadcaster:
    """Coalesce high-frequency ticks and fan out only the latest values."""

    def __init__(self, interval_milliseconds: int) -> None:
        self._clients: set[WebSocket] = set()
        self._pending: dict[str, Tick] = {}
        self._interval_seconds = interval_milliseconds / 1000
        self._event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="frontend-tick-broadcaster")

    async def stop(self) -> None:
        self._stop_event.set()
        self._event.set()
        if self._task is not None:
            await self._task
        self._task = None

    def publish(self, tick: Tick) -> None:
        """Replace a symbol's pending tick in expected O(1) time."""
        self._pending[tick.symbol] = tick
        self._event.set()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self._event.wait()
            self._event.clear()
            await asyncio.sleep(self._interval_seconds)
            if self._stop_event.is_set():
                break
            ticks = tuple(self._pending.values())
            self._pending.clear()
            if ticks:
                await self._broadcast(ticks)

    async def _broadcast(self, ticks: tuple[Tick, ...]) -> None:
        """Send one compact batch to each client."""
        clients = tuple(self._clients)
        if not clients:
            return

        payload = {
            "type": "ticks",
            "data": [tick.model_dump(mode="json") for tick in ticks],
        }
        results = await asyncio.gather(
            *(client.send_json(payload) for client in clients),
            return_exceptions=True,
        )
        failed = [
            client for client, result in zip(clients, results, strict=True)
            if isinstance(result, Exception)
        ]
        if failed:
            for client in failed:
                self._clients.discard(client)
            logger.info("stale_frontend_clients_removed count=%s", len(failed))
