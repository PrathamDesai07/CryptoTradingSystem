"""Typed configuration loaded from YAML with environment overrides."""

from enum import StrEnum
from functools import lru_cache
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator
import yaml


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TEST = "test"


class Settings(BaseModel):
    """Validated runtime settings. Values are supplied by the YAML loader."""

    app_environment: AppEnvironment
    app_title: str
    root_message: str
    market_data_label: str
    app_host: str
    app_port: int = Field(ge=1, le=65535)
    log_level: str
    api_prefix: str
    frontend_mount_path: str
    open_browser_on_start: bool
    browser_open_delay_seconds: float = Field(ge=0)
    docs_url: str
    redoc_url: str
    cors_origins: str
    cors_allow_credentials: bool
    cors_allow_methods: list[str]
    cors_allow_headers: list[str]

    binance_api_key: SecretStr | None
    binance_api_secret: SecretStr | None
    binance_testnet_rest_url: str
    binance_market_rest_url: str
    binance_market_ws_url: str

    trading_symbols: str
    market_data_enabled: bool
    market_stream_suffix: str
    order_book_stream_suffix: str
    order_book_depth_levels: int = Field(ge=5, le=20)
    candle_history_size: int = Field(ge=1, le=10000)
    candle_history_bootstrap_limit: int = Field(ge=1, le=1000)
    candle_history_request_timeout_seconds: float = Field(gt=0)
    candle_interval_name: str
    candle_interval_seconds: int = Field(ge=1)
    chart_intervals: list[str]
    chart_history_limit: int = Field(ge=20, le=1000)
    websocket_open_timeout_seconds: float = Field(gt=0)
    websocket_close_timeout_seconds: float = Field(gt=0)
    websocket_ping_interval_seconds: float = Field(gt=0)
    websocket_ping_timeout_seconds: float = Field(gt=0)
    websocket_stale_timeout_seconds: float = Field(gt=0)
    websocket_reconnect_initial_seconds: float = Field(gt=0)
    websocket_reconnect_max_seconds: float = Field(gt=0)
    websocket_reconnect_jitter_ratio: float = Field(ge=0, le=1)
    websocket_control_interval_seconds: float = Field(gt=0)
    websocket_max_streams: int = Field(ge=1, le=1024)
    websocket_max_message_bytes: int = Field(gt=0)
    websocket_max_queue: int = Field(gt=0)
    frontend_tick_poll_seconds: float = Field(gt=0)
    frontend_broadcast_interval_milliseconds: int = Field(ge=16, le=5000)
    order_book_render_interval_milliseconds: int = Field(ge=100, le=5000)
    order_size_usdt: float = Field(gt=0)
    max_order_balance_utilization_percent: float = Field(gt=0, le=100)
    strategy_order_quantity: float = Field(gt=0)
    order_request_timeout_seconds: float = Field(gt=0, le=60)
    order_recv_window_milliseconds: int = Field(ge=1, le=60000)
    exchange_info_cache_seconds: int = Field(ge=1, le=86400)
    state_database_path: str
    fast_sma_period: int = Field(ge=1)
    slow_ema_period: int = Field(ge=2)
    strategy_signal_history_size: int = Field(ge=1, le=10000)
    strategy_indicator_history_size: int = Field(ge=1, le=10000)
    variant_a_stop_loss_percent: float = Field(gt=0, lt=100)
    variant_b_stop_loss_percent: float = Field(gt=0, lt=100)
    take_profit_percent: float = Field(gt=0)
    order_execution_enabled: bool
    # Base64-encoded 32-byte Fernet key used to encrypt stored Binance Demo
    # credentials. When absent the key is auto-generated next to the database.
    credentials_master_key: SecretStr | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("trading_symbols")
    @classmethod
    def validate_symbols(cls, value: str) -> str:
        symbols = [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
        if not symbols:
            raise ValueError("TRADING_SYMBOLS must contain at least one symbol")
        if any(not symbol.isalnum() for symbol in symbols):
            raise ValueError("TRADING_SYMBOLS may contain only letters and numbers")
        return ",".join(dict.fromkeys(symbols))

    @field_validator("market_stream_suffix", "order_book_stream_suffix")
    @classmethod
    def validate_stream_suffix(cls, value: str) -> str:
        if not value.startswith("@") or len(value) < 2:
            raise ValueError("MARKET_STREAM_SUFFIX must start with @")
        return value

    @field_validator("binance_market_ws_url")
    @classmethod
    def validate_market_stream_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        allowed_hosts = (
            "wss://stream.binance.com",
            "wss://stream.testnet.binance.vision",
        )
        if not normalized.startswith(allowed_hosts):
            raise ValueError("BINANCE_MARKET_WS_URL must use an official Binance stream")
        return normalized

    @field_validator("binance_testnet_rest_url")
    @classmethod
    def validate_order_rest_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if normalized not in {
            "https://testnet.binance.vision",
            "https://demo-api.binance.com",
        }:
            raise ValueError("BINANCE_TESTNET_REST_URL must use Binance Spot Testnet or Demo Mode")
        return normalized

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.trading_symbols.split(","))

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_environment is AppEnvironment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Load YAML, expand secret references, apply environment overrides, validate."""
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    config_path = Path(os.getenv("CONFIG_FILE", project_root / "config.yaml"))
    if not config_path.is_absolute():
        config_path = project_root / config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file)
    if not isinstance(raw_config, dict):
        raise ValueError("The YAML configuration root must be a mapping")

    expanded = {key: _expand_environment(value) for key, value in raw_config.items()}
    for key in tuple(expanded):
        environment_value = os.getenv(key.upper())
        if environment_value is not None:
            expanded[key] = environment_value

    return Settings.model_validate(expanded)


_ENV_REFERENCE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}$")


def _expand_environment(value: Any) -> Any:
    """Resolve a complete `${NAME}` or `${NAME:-fallback}` YAML value."""
    if not isinstance(value, str):
        return value
    match = _ENV_REFERENCE.fullmatch(value)
    if not match:
        return value
    variable, fallback = match.groups()
    resolved = os.getenv(variable, fallback)
    return resolved or None
