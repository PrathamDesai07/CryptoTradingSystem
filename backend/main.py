"""Application entry point that connects the backend modules."""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from config import get_settings
from logging_config import configure_logging

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "application_started environment=%s order_execution_enabled=%s",
        settings.app_environment,
        settings.order_execution_enabled,
    )
    yield
    logger.info("application_stopped")


app = FastAPI(
    title=settings.app_title,
    docs_url=None if settings.is_production else settings.docs_url,
    redoc_url=None if settings.is_production else settings.redoc_url,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)

app.include_router(router, prefix=settings.api_prefix)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": settings.root_message}
