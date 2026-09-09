"""REST and WebSocket endpoints exposed to the frontend."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from models.common import normalize_symbol

router = APIRouter()
websocket_router = APIRouter()


class SymbolRequest(BaseModel):
    symbol: str


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    client = request.app.state.market_data_client
    return {
        "status": "ok",
        "market_data_enabled": request.app.state.settings.market_data_enabled,
        "binance_connected": client.connected,
        "last_message_at": client.last_message_at,
        "frontend_clients": request.app.state.tick_broadcaster.client_count,
        "server_time": datetime.now(UTC),
    }


@router.get("/config")
async def public_config(request: Request) -> dict[str, object]:
    """Return only browser-safe configuration; never expose credentials."""
    settings = request.app.state.settings
    return {
        "app_title": settings.app_title,
        "market_data_label": settings.market_data_label,
        "tick_poll_seconds": settings.frontend_tick_poll_seconds,
        "order_execution_enabled": settings.order_execution_enabled,
    }


@router.get("/symbols")
async def list_symbols(request: Request) -> dict[str, object]:
    return {"symbols": await request.app.state.market_data_client.active_symbols()}


@router.post("/symbols", status_code=201)
async def add_symbol(payload: SymbolRequest, request: Request) -> dict[str, object]:
    try:
        symbol = normalize_symbol(payload.symbol)
        added = await request.app.state.market_data_client.add_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"symbol": symbol, "added": added}


@router.delete("/symbols/{symbol}")
async def remove_symbol(symbol: str, request: Request) -> dict[str, object]:
    try:
        normalized = normalize_symbol(symbol)
        removed = await request.app.state.market_data_client.remove_symbol(normalized)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not removed:
        raise HTTPException(status_code=404, detail="symbol is not active")
    return {"symbol": normalized, "removed": True}


@router.get("/ticks/latest")
async def latest_ticks(request: Request) -> dict[str, object]:
    ticks = await request.app.state.tick_store.snapshot()
    return {"ticks": ticks, "count": len(ticks)}


@router.get("/ticks/latest/{symbol}")
async def latest_tick(symbol: str, request: Request) -> dict[str, object]:
    try:
        normalized = normalize_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    tick = await request.app.state.tick_store.get(normalized)
    if tick is None:
        raise HTTPException(status_code=404, detail="no tick available for symbol")
    return {"tick": tick}


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, object]:
    """Return the currently implemented dashboard state in one request."""
    client = request.app.state.market_data_client
    ticks = await request.app.state.tick_store.snapshot()
    return {
        "health": {
            "binance_connected": client.connected,
            "last_message_at": client.last_message_at,
        },
        "symbols": await client.active_symbols(),
        "ticks": ticks,
        "features": {
            "market_data": "complete",
            "candles": "planned",
            "strategy": "planned",
            "orders": "planned",
        },
    }


@websocket_router.websocket("/ws/ticks")
async def tick_stream(websocket: WebSocket) -> None:
    broadcaster = websocket.app.state.tick_broadcaster
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(websocket)
