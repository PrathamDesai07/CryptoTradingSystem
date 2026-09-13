"""Application entry point that connects the backend modules."""

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.auth_routes import router as auth_router
from api.routes import router, websocket_router
from config import get_settings
from logging_config import configure_logging
from models import Candle, Signal, Tick
from services import (
    BinanceStreamClient,
    CandleService,
    DatabaseHandler,
    OrderBookStore,
    OrderService,
    PreTradeRiskEngine,
    ReconciliationService,
    StrategyService,
    TickBroadcaster,
    TickStore,
)

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
tick_store = TickStore()
order_book_store = OrderBookStore()
tick_broadcaster = TickBroadcaster(settings.frontend_broadcast_interval_milliseconds)
db_handler = DatabaseHandler(
    settings.state_database_path,
    settings.strategy_signal_history_size,
    master_key=settings.credentials_master_key.get_secret_value() if settings.credentials_master_key else None,
    database_url=settings.database_url.get_secret_value() if settings.database_url else None,
    database_password=settings.database_password.get_secret_value() if settings.database_password else None,
)
risk_engine = PreTradeRiskEngine(
    Decimal(str(settings.max_order_balance_utilization_percent)),
    Decimal(str(settings.max_order_notional_usdt)),
    repository=db_handler,
)
reconciliation_service = ReconciliationService()
order_service = OrderService(
    settings,
    repository=db_handler,
    risk_engine=risk_engine,
    reconciliation_service=reconciliation_service,
    market_tick_loader=tick_store.get,
)


async def handle_signal(signal: Signal) -> None:
    tick_broadcaster.publish_signal(signal)
    user_ids = order_service.active_strategy_user_ids()
    if user_ids:
        for user_id in user_ids:
            order_service.create_background_task(
                order_service.process_signal_for_user(user_id, signal),
                name=f"strategy-order-user-{user_id}-{signal.symbol}-{signal.variant.value}",
            )
    else:
        order_service.create_background_task(
            order_service.process_signal(signal),
            name=f"strategy-order-{signal.symbol}-{signal.variant.value}",
        )


strategy_service = StrategyService(
    settings.fast_sma_period,
    settings.slow_ema_period,
    settings.strategy_signal_history_size,
    settings.strategy_indicator_history_size,
    handle_signal,
    tick_broadcaster.publish_indicator,
)
order_service.set_strategy_relation_loader(strategy_service.relation)


async def handle_candle(candle: Candle) -> None:
    tick_broadcaster.publish_candle(candle)
    if candle.is_final:
        await strategy_service.process_candle(candle)


candle_service = CandleService(
    settings.candle_history_size,
    settings.candle_interval_seconds,
    handle_candle,
)


async def handle_tick(tick: Tick) -> None:
    tick_broadcaster.publish(tick)
    await candle_service.process_tick(tick)
    await order_service.process_tick(tick)


market_data_client = BinanceStreamClient(
    settings,
    tick_store,
    handle_tick,
    order_book_store,
    tick_broadcaster.publish_order_book,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not db_handler.acquire_execution_lease():
        raise RuntimeError(
            "another Crypto Trading System process already owns the execution lease; "
            "run exactly one Uvicorn worker per database"
        )
    logger.info(
        "application_started environment=%s order_execution_enabled=%s",
        settings.app_environment,
        settings.order_execution_enabled,
    )
    try:
        await tick_broadcaster.start()
        await candle_service.start()
        order_service.start_background_services()
        if settings.market_data_enabled:
            await asyncio.gather(
                *(
                    candle_service.bootstrap_history(
                        symbol,
                        settings.binance_market_rest_url,
                        settings.candle_interval_name,
                        settings.candle_history_bootstrap_limit,
                        settings.candle_history_request_timeout_seconds,
                    )
                    for symbol in settings.symbols
                )
            )
            for symbol in settings.symbols:
                await strategy_service.seed(
                    symbol,
                    await candle_service.history(
                        symbol, settings.candle_history_bootstrap_limit
                    ),
                )
            await market_data_client.start()
        yield
    finally:
        await market_data_client.stop()
        await order_service.shutdown()
        order_service.clear_credentials()
        await candle_service.stop()
        await tick_broadcaster.stop()
        db_handler.release_execution_lease()
        logger.info("application_stopped")


app = FastAPI(
    title=settings.app_title,
    docs_url=None if settings.is_production else settings.docs_url,
    redoc_url=None if settings.is_production else settings.redoc_url,
    lifespan=lifespan,
)
app.state.settings = settings
app.state.tick_store = tick_store
app.state.order_book_store = order_book_store
app.state.tick_broadcaster = tick_broadcaster
app.state.market_data_client = market_data_client
app.state.candle_service = candle_service
app.state.strategy_service = strategy_service
app.state.order_service = order_service
app.state.db_handler = db_handler

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"https://[A-Za-z0-9-]+\.vercel\.app",
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)

app.include_router(router, prefix=settings.api_prefix)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(websocket_router)

if settings.frontend_serving_enabled:
    frontend_directory = Path(__file__).resolve().parent.parent / "frontend"
    app.mount(
        settings.frontend_mount_path,
        StaticFiles(directory=frontend_directory, html=True),
        name="frontend",
    )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(frontend_directory / "favicon.svg", media_type="image/svg+xml")

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.frontend_mount_path}/")
else:

    @app.get("/")
    def root() -> dict[str, str]:
        """API-only mode: the Vite frontend runs as a separate process."""
        return {"message": settings.root_message}
