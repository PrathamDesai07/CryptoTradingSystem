"""REST and WebSocket endpoints exposed to the frontend."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, model_validator

from api.security import require_user
from models.common import StrategyVariant, normalize_symbol
from services import BinanceOrderError

router = APIRouter()
websocket_router = APIRouter()


class SymbolRequest(BaseModel):
    symbol: str


class SpotOrderRequest(BaseModel):
    """All parameters supported by Binance's seven single Spot order types."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: str
    type: str
    timeInForce: str | None = None
    quantity: str | None = None
    quoteOrderQty: str | None = None
    price: str | None = None
    newClientOrderId: str | None = None
    strategyId: int | None = None
    strategyType: int | None = None
    stopPrice: str | None = None
    trailingDelta: int | None = None
    icebergQty: str | None = None
    newOrderRespType: str | None = "FULL"
    selfTradePreventionMode: str | None = None
    pegPriceType: str | None = None
    pegOffsetValue: int | None = None
    pegOffsetType: str | None = None

    @model_validator(mode="after")
    def normalize_enums(self) -> "SpotOrderRequest":
        self.symbol = normalize_symbol(self.symbol)
        self.side = self.side.upper()
        self.type = self.type.upper()
        if self.timeInForce:
            self.timeInForce = self.timeInForce.upper()
        return self


class OrderReferenceRequest(BaseModel):
    symbol: str
    order_id: int | None = None
    client_order_id: str | None = None


class RuntimeCredentialsRequest(BaseModel):
    api_key: str
    api_secret: str


class StrategyOrderQuantityRequest(BaseModel):
    quantity: Decimal


class OrderListRequest(BaseModel):
    type: str
    parameters: dict[str, object]


def order_error(error: BinanceOrderError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"message": str(error), "binance_code": error.code})


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
        "order_book_render_interval_milliseconds": (
            settings.order_book_render_interval_milliseconds
        ),
        "fast_sma_period": settings.fast_sma_period,
        "slow_ema_period": settings.slow_ema_period,
        "chart_intervals": settings.chart_intervals,
        "order_execution_enabled": settings.order_execution_enabled,
        "strategy_order_quantity": request.app.state.order_service.strategy_order_quantity,
    }


@router.put("/strategy/order-quantity")
async def update_strategy_order_quantity(
    payload: StrategyOrderQuantityRequest, request: Request
) -> dict[str, object]:
    """Change sizing for future automatic strategy orders in this process."""
    try:
        quantity = await request.app.state.order_service.set_strategy_order_quantity(
            payload.quantity
        )
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"quantity": quantity, "persistence": "until_backend_restart"}


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
    if added:
        settings = request.app.state.settings
        await request.app.state.candle_service.bootstrap_history(
            symbol,
            settings.binance_market_rest_url,
            settings.candle_interval_name,
            settings.candle_history_bootstrap_limit,
            settings.candle_history_request_timeout_seconds,
        )
        await request.app.state.strategy_service.seed(
            symbol,
            await request.app.state.candle_service.history(
                symbol, settings.candle_history_bootstrap_limit
            ),
        )
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
    await request.app.state.candle_service.remove_symbol(normalized)
    await request.app.state.strategy_service.remove_symbol(normalized)
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


@router.get("/candles/{symbol}")
async def candles(
    symbol: str,
    request: Request,
    limit: int = Query(default=60, ge=1, le=1000),
) -> dict[str, object]:
    try:
        normalized = normalize_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    service = request.app.state.candle_service
    return {
        "symbol": normalized,
        "current": await service.current(normalized),
        "history": await service.history(normalized, limit),
    }


@router.get("/chart-candles/{symbol}")
async def chart_candles(
    symbol: str,
    request: Request,
    interval: str,
) -> dict[str, object]:
    try:
        normalized = normalize_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    settings = request.app.state.settings
    if interval not in settings.chart_intervals:
        raise HTTPException(status_code=422, detail="unsupported chart interval")
    candles = await request.app.state.candle_service.chart_history(
        normalized,
        settings.binance_market_rest_url,
        interval,
        settings.chart_history_limit,
        settings.candle_history_request_timeout_seconds,
    )
    return {"symbol": normalized, "interval": interval, "candles": candles}


@router.get("/order-book/{symbol}")
async def order_book(symbol: str, request: Request) -> dict[str, object]:
    try:
        normalized = normalize_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    book = await request.app.state.order_book_store.get(normalized)
    if book is None:
        raise HTTPException(
            status_code=404,
            detail="order book is not subscribed or no snapshot has arrived",
        )
    return {"order_book": book}


@router.get("/indicators/{symbol}")
async def indicators(
    symbol: str,
    request: Request,
    limit: int = Query(default=120, ge=1, le=1000),
) -> dict[str, object]:
    try:
        normalized = normalize_symbol(symbol)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    settings = request.app.state.settings
    return {
        "symbol": normalized,
        "fast_period": settings.fast_sma_period,
        "slow_period": settings.slow_ema_period,
        "indicators": await request.app.state.strategy_service.indicators(
            normalized, limit
        ),
        "latest_signals": await request.app.state.strategy_service.latest_signals(
            normalized
        ),
    }


@router.get("/signals")
async def signals(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    normalized = None
    if symbol is not None:
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    items = await request.app.state.strategy_service.signals(normalized, limit)
    return {"signals": items, "count": len(items)}


@router.get("/positions")
async def positions(request: Request, symbol: str | None = None, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    normalized = normalize_symbol(symbol) if symbol else None
    items = await request.app.state.order_service.positions(normalized)
    return {"positions": items, "count": len(items)}


@router.get("/order-session")
async def order_session(request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    """Per-account session status; keys are stored encrypted in SQLite."""
    db = request.app.state.db_handler
    service = request.app.state.order_service
    has_stored = db.get_user_credentials(int(user["id"])) is not None
    return {
        "username": user["username"],
        "credentials_configured": has_stored or service.credentials_configured,
        "verified": service.session_verified,
        "runtime_session_active": service.runtime_session_active,
        "execution_enabled": service.execution_enabled,
        "has_stored_credentials": has_stored,
        "storage": "encrypted_database",
    }


@router.post("/order-session")
async def connect_order_session(payload: RuntimeCredentialsRequest, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    """Replace this account's stored keys and reconnect the live session."""
    db = request.app.state.db_handler
    service = request.app.state.order_service
    api_key, api_secret = payload.api_key.strip(), payload.api_secret.strip()
    if not api_key or not api_secret:
        raise HTTPException(status_code=422, detail="API key and secret are required")
    db.set_user_credentials(int(user["id"]), db.encrypt_secret(api_key), db.encrypt_secret(api_secret))
    try:
        status = await service.connect_user_credentials(int(user["id"]), api_key, api_secret)
    except BinanceOrderError as error:
        raise order_error(error) from error
    if status.get("verified") and status.get("can_trade"):
        db.mark_credentials_verified(int(user["id"]))
        return {"connected": True, "storage": "encrypted_database", "account": {"can_trade": True}}
    raise order_error(BinanceOrderError(status.get("error") or "Binance Demo keys could not be verified", 403 if status.get("verified") else 502))


@router.delete("/order-session")
async def disconnect_order_session(request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    request.app.state.order_service.deactivate_user_session(int(user["id"]))
    return {"connected": False, "storage": "encrypted_database"}


@router.post("/positions/{symbol}/{variant}/square-off")
async def square_off(symbol: str, variant: StrategyVariant, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        await request.app.state.order_service.square_off(normalize_symbol(symbol), variant)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"symbol": normalize_symbol(symbol), "variant": variant, "status": "square-off submitted"}


@router.get("/pnl/{symbol}")
async def pnl(symbol: str, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    normalized = normalize_symbol(symbol)
    tick = await request.app.state.tick_store.get(normalized)
    if tick is None:
        raise HTTPException(status_code=404, detail="no live price is available for this symbol")
    try:
        return await request.app.state.order_service.pnl(normalized, tick.price)
    except BinanceOrderError as error:
        raise order_error(error) from error


@router.post("/orders/{symbol}/{order_id}/square-off")
async def square_off_order(symbol: str, order_id: int, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    normalized = normalize_symbol(symbol)
    try:
        service = request.app.state.order_service
        result = await service.square_off_order(normalized, order_id)
    except BinanceOrderError as error:
        raise order_error(error) from error
    rules = await service._rules(normalized)
    quote_asset = str(rules.get("_quoteAsset") or "USDT")
    gross_quote = Decimal(str(result.get("cummulativeQuoteQty", "0")))
    quote_commission = sum(
        (
            Decimal(str(fill.get("commission", "0")))
            for fill in result.get("fills", [])
            if fill.get("commissionAsset") == quote_asset
        ),
        Decimal(0),
    )
    return {
        "order": result,
        "settlement": {
            "base_quantity_sold": Decimal(str(result.get("executedQty", "0"))),
            "quote_asset": quote_asset,
            "quote_credited": gross_quote - quote_commission,
        },
    }


@router.get("/orders")
async def local_orders(
    request: Request,
    symbol: str | None = None,
    side: str | None = None,
    status: str | None = None,
    strategy_variant: StrategyVariant | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object]:
    normalized = normalize_symbol(symbol) if symbol else None
    if any(value is not None and value.tzinfo is None for value in (start_time, end_time)):
        raise HTTPException(status_code=422, detail="start_time and end_time must include a timezone")
    service = request.app.state.order_service
    try:
        items = (
            await service.account_orders(normalized, limit)
            if normalized and service.credentials_configured
            else await service.order_log(normalized, limit)
        )
    except BinanceOrderError as error:
        raise order_error(error) from error
    filtered = []
    for item in items:
        if side and str(item.get("side", "")).upper() != side.upper():
            continue
        if status and str(item.get("status", "")).upper() != status.upper():
            continue
        if strategy_variant and item.get("strategy_variant") != strategy_variant.value:
            continue
        raw_time = item.get("time") or item.get("transactTime") or item.get("recordedAt")
        item_time = None
        if isinstance(raw_time, (int, float)):
            item_time = datetime.fromtimestamp(raw_time / 1000, tz=UTC)
        elif isinstance(raw_time, str):
            try:
                item_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                pass
        if start_time and item_time and item_time < start_time:
            continue
        if end_time and item_time and item_time > end_time:
            continue
        filtered.append(item)
    return {"orders": filtered[-limit:], "count": len(filtered[-limit:])}


@router.post("/orders")
async def create_order(payload: SpotOrderRequest, request: Request, test: bool = False, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        result = await request.app.state.order_service.place_order(payload.model_dump(exclude_none=True), test)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"test": test, "order": result}


@router.get("/orders/open")
async def open_orders(request: Request, symbol: str | None = None, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        items = await request.app.state.order_service.open_orders(normalize_symbol(symbol) if symbol else None)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"orders": items, "count": len(items)}


@router.post("/order-lists")
async def create_order_list(payload: OrderListRequest, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        result = await request.app.state.order_service.place_order_list(payload.type, payload.parameters)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"order_list": result}


@router.get("/order-lists")
async def order_lists(request: Request, open_only: bool = True, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        items = await request.app.state.order_service.order_lists(open_only)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"order_lists": items, "count": len(items)}


@router.get("/order-lists/status")
async def order_list_status(request: Request, order_list_id: int | None = None, client_order_id: str | None = None, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        result = await request.app.state.order_service.query_order_list(order_list_id, client_order_id)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"order_list": result}


@router.delete("/order-lists")
async def cancel_order_list(payload: OrderReferenceRequest, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        result = await request.app.state.order_service.cancel_order_list(normalize_symbol(payload.symbol), payload.order_id, payload.client_order_id)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"order_list": result}


@router.get("/orders/status")
async def order_status(request: Request, symbol: str, order_id: int | None = None, client_order_id: str | None = None, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        result = await request.app.state.order_service.query_order(normalize_symbol(symbol), order_id, client_order_id)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"order": result}


@router.delete("/orders")
async def cancel_order(payload: OrderReferenceRequest, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        result = await request.app.state.order_service.cancel_order(normalize_symbol(payload.symbol), payload.order_id, payload.client_order_id)
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"order": result}


@router.delete("/orders/open/{symbol}")
async def cancel_all_orders(symbol: str, request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        items = await request.app.state.order_service.cancel_all(normalize_symbol(symbol))
    except BinanceOrderError as error:
        raise order_error(error) from error
    return {"orders": items, "count": len(items)}


@router.get("/account")
async def account(request: Request, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    try:
        return {"account": await request.app.state.order_service.account()}
    except BinanceOrderError as error:
        raise order_error(error) from error


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, object]:
    """Return the currently implemented dashboard state in one request."""
    client = request.app.state.market_data_client
    ticks = await request.app.state.tick_store.snapshot()
    symbols = await client.active_symbols()
    current_candles = {
        symbol: candle
        for symbol in symbols
        if (candle := await request.app.state.candle_service.current(symbol)) is not None
    }
    return {
        "health": {
            "binance_connected": client.connected,
            "last_message_at": client.last_message_at,
        },
        "symbols": symbols,
        "ticks": ticks,
        "candles": current_candles,
        "features": {
            "market_data": "complete",
            "candles": "complete",
            "strategy": "complete",
            "risk_management": "complete",
            "orders": "complete",
        },
    }


@websocket_router.websocket("/ws/ticks")
async def tick_stream(websocket: WebSocket) -> None:
    broadcaster = websocket.app.state.tick_broadcaster
    market_client = websocket.app.state.market_data_client
    await broadcaster.connect(websocket)
    try:
        while True:
            message = await websocket.receive_json()
            action = message.get("action") if isinstance(message, dict) else None
            try:
                symbol = normalize_symbol(message.get("symbol", ""))
            except (AttributeError, ValueError) as error:
                await websocket.send_json({"type": "error", "message": str(error)})
                continue

            if action == "subscribe_depth":
                first_viewer = broadcaster.subscribe_depth(websocket, symbol)
                try:
                    if first_viewer:
                        await market_client.add_depth_symbol(symbol)
                    await websocket.send_json(
                        {"type": "depth_subscription", "symbol": symbol, "active": True}
                    )
                except ValueError as error:
                    broadcaster.unsubscribe_depth(websocket, symbol)
                    await websocket.send_json({"type": "error", "message": str(error)})
            elif action == "unsubscribe_depth":
                last_viewer = broadcaster.unsubscribe_depth(websocket, symbol)
                if last_viewer:
                    await market_client.remove_depth_symbol(symbol)
                await websocket.send_json(
                    {"type": "depth_subscription", "symbol": symbol, "active": False}
                )
            else:
                await websocket.send_json(
                    {"type": "error", "message": "unsupported WebSocket action"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        depth_symbols = broadcaster.depth_symbols_for(websocket)
        for symbol in depth_symbols:
            if broadcaster.unsubscribe_depth(websocket, symbol):
                await market_client.remove_depth_symbol(symbol)
        await broadcaster.disconnect(websocket)
