"""Application entry point that connects the backend modules."""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.routes import router, websocket_router
from config import get_settings
from logging_config import configure_logging
from services import BinanceStreamClient, TickBroadcaster, TickStore

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
tick_store = TickStore()
tick_broadcaster = TickBroadcaster(settings.frontend_broadcast_interval_milliseconds)
market_data_client = BinanceStreamClient(settings, tick_store, tick_broadcaster.publish)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "application_started environment=%s order_execution_enabled=%s",
        settings.app_environment,
        settings.order_execution_enabled,
    )
    await tick_broadcaster.start()
    if settings.market_data_enabled:
        await market_data_client.start()
    yield
    await market_data_client.stop()
    await tick_broadcaster.stop()
    logger.info("application_stopped")


app = FastAPI(
    title=settings.app_title,
    docs_url=None if settings.is_production else settings.docs_url,
    redoc_url=None if settings.is_production else settings.redoc_url,
    lifespan=lifespan,
)
app.state.settings = settings
app.state.tick_store = tick_store
app.state.tick_broadcaster = tick_broadcaster
app.state.market_data_client = market_data_client

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)

app.include_router(router, prefix=settings.api_prefix)
app.include_router(websocket_router)

frontend_directory = Path(__file__).resolve().parent.parent / "frontend"
app.mount(
    settings.frontend_mount_path,
    StaticFiles(directory=frontend_directory, html=True),
    name="frontend",
)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url=f"{settings.frontend_mount_path}/")
