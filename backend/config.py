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
    app_host: str
    app_port: int = Field(ge=1, le=65535)
    log_level: str
    api_prefix: str
    docs_url: str
    redoc_url: str
    cors_origins: str
    cors_allow_credentials: bool
    cors_allow_methods: list[str]
    cors_allow_headers: list[str]

    binance_api_key: SecretStr | None
    binance_api_secret: SecretStr | None
    binance_testnet_rest_url: str
    binance_testnet_ws_url: str

    trading_symbols: str
    order_size_usdt: float = Field(gt=0)
    fast_sma_period: int = Field(ge=1)
    slow_ema_period: int = Field(ge=2)
    variant_a_stop_loss_percent: float = Field(gt=0, lt=100)
    variant_b_stop_loss_percent: float = Field(gt=0, lt=100)
    take_profit_percent: float = Field(gt=0)
    order_execution_enabled: bool

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
