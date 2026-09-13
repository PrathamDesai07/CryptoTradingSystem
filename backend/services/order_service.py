"""Signed Binance Spot Demo orders and independent strategy position risk."""

import asyncio
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import json
import logging
from time import monotonic, time
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import SecretStr

from config import Settings
from models import ExitReason, Position, PositionStatus, Signal, SignalAction, StrategyVariant, Tick
from services.state_repository import StateRepository
from services.pretrade_risk import PreTradeRiskEngine, PreTradeRiskError
from services.reconciliation_service import ReconciliationService

logger = logging.getLogger(__name__)

# The user whose Binance Demo credentials sign the current request. Background
# strategy/risk tasks run without a bound user and keep using process-level
# credentials, exactly as before multi-user support.
current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
PositionKey = tuple[int | None, str, StrategyVariant]


class _UserSession:
    """Decrypted in-memory credentials for one logged-in account."""

    __slots__ = ("api_key", "api_secret", "verified", "enabled")

    def __init__(self, api_key: SecretStr, api_secret: SecretStr) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.verified = False
        self.enabled = False


class BinanceOrderError(RuntimeError):
    """A credential-free Binance API error safe to return to clients."""

    def __init__(self, message: str, status_code: int = 502, code: int | None = None, uncertain: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.uncertain = uncertain


class OrderService:
    """Manage Spot orders and O(1) position lookup by symbol and variant."""

    ORDER_TYPES = frozenset({"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "LIMIT_MAKER"})
    TIME_IN_FORCE = frozenset({"GTC", "IOC", "FOK"})
    ORDER_LIST_PATHS = {
        "OCO": "/api/v3/orderList/oco",
        "OTO": "/api/v3/orderList/oto",
        "OTOCO": "/api/v3/orderList/otoco",
        "OPO": "/api/v3/orderList/opo",
        "OPOCO": "/api/v3/orderList/opoco",
    }

    def __init__(self, settings: Settings, repository: Any | None = None,
                 risk_engine: PreTradeRiskEngine | None = None,
                 reconciliation_service: ReconciliationService | None = None,
                 market_tick_loader: Callable[[str], Awaitable[Tick | None]] | None = None,
                 strategy_relation_loader: Callable[[str], Awaitable[int | None]] | None = None) -> None:
        self._settings = settings
        self._positions: dict[PositionKey, Position] = {}
        self._positions_by_symbol: dict[str, set[PositionKey]] = {}
        self._strategy_enabled_users: dict[int, bool] = {}
        self._strategy_status: dict[int, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=settings.strategy_signal_history_size)
        self._symbol_rules: dict[str, dict[str, Any]] = {}
        self._exchange_info_loaded_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._inflight: set[PositionKey] = set()
        self._pending_entries: set[PositionKey] = set()
        self._runtime_api_key: SecretStr | None = None
        self._runtime_api_secret: SecretStr | None = None
        self._session_execution_enabled = False
        self._user_sessions: dict[int, _UserSession] = {}
        self._strategy_order_quantity = Decimal(str(settings.strategy_order_quantity))
        self._squaring_orders: set[tuple[str, int]] = set()
        self._reconciled_positions: set[PositionKey] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._request_latencies_ms: deque[float] = deque(maxlen=1024)
        self._metric_counts: dict[str, int] = {"requests": 0, "errors": 0, "risk_rejections": 0, "reconciliations": 0}
        self._request_semaphore = asyncio.Semaphore(settings.max_concurrent_exchange_requests)
        self._consecutive_order_failures = 0
        self._circuit_open_until = 0.0
        self._shutdown_event = asyncio.Event()
        self._repository = repository or StateRepository(settings.state_database_path, settings.strategy_signal_history_size)
        self._risk = risk_engine or PreTradeRiskEngine(
            Decimal(str(settings.max_order_balance_utilization_percent)),
            Decimal(str(settings.max_order_notional_usdt)),
        )
        self._reconciliation = reconciliation_service or ReconciliationService()
        self._market_tick_loader = market_tick_loader
        self._strategy_relation_loader = strategy_relation_loader
        for record in self._repository.load_orders():
            self._history.append(record)
            client_id = str(record.get("clientOrderId") or record.get("newClientOrderId") or "")
            if client_id:
                self._orders[client_id] = record
        for position in self._repository.load_positions():
            key = (None, position.symbol, position.variant)
            self._positions[key] = position
            self._positions_by_symbol.setdefault(position.symbol, set()).add(key)
        if hasattr(self._repository, "load_user_positions"):
            for user_id, position in self._repository.load_user_positions():
                key = (user_id, position.symbol, position.variant)
                self._positions[key] = position
                self._positions_by_symbol.setdefault(position.symbol, set()).add(key)
                if position.status is PositionStatus.CLOSED and hasattr(self._repository, "record_position_history"):
                    self._repository.record_position_history(user_id, position, "RESTORED")

    def create_background_task(self, coroutine: Any, *, name: str) -> asyncio.Task[Any]:
        """Create and retain an execution task until it reaches a terminal state."""
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def set_strategy_relation_loader(self, loader: Callable[[str], Awaitable[int | None]] | None) -> None:
        """Attach the strategy's standing SMA/EMA relation lookup after construction."""
        self._strategy_relation_loader = loader

    async def shutdown(self) -> None:
        """Drain submitted execution tasks before process shutdown."""
        self._shutdown_event.set()
        tasks = tuple(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def start_background_services(self) -> None:
        self._shutdown_event.clear()
        self.create_background_task(self._reconciliation_loop(), name="order-reconciliation-loop")

    async def _reconciliation_loop(self) -> None:
        while not self._shutdown_event.is_set():
            for user_id in self.active_execution_user_ids():
                await self._reconcile_account_reservations(user_id)
                await self._reconcile_strategy_positions(user_id)
            if self._settings.order_execution_enabled:
                await self._reconcile_strategy_positions(None)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._settings.reconciliation_interval_seconds,
                )
            except TimeoutError:
                pass

    @property
    def execution_enabled(self) -> bool:
        """Per-user kill switch when a request is bound, process-level otherwise."""
        if current_user_id.get() is not None:
            session = self._session()
            return bool(session and session.enabled)
        return self._settings.order_execution_enabled or self._session_execution_enabled

    @property
    def credentials_configured(self) -> bool:
        if current_user_id.get() is not None:
            return self._session() is not None
        return bool(self._runtime_api_key or self._settings.binance_api_key) and bool(
            self._runtime_api_secret or self._settings.binance_api_secret
        )

    @property
    def runtime_session_active(self) -> bool:
        if current_user_id.get() is not None:
            session = self._session()
            return bool(session and session.verified and session.enabled)
        return bool(self._runtime_api_key and self._runtime_api_secret and self._session_execution_enabled)

    @property
    def session_verified(self) -> bool:
        session = self._session()
        return bool(session and session.verified)

    def _session(self, user_id: int | None = None) -> _UserSession | None:
        if user_id is None:
            user_id = current_user_id.get()
        if user_id is None:
            return None
        return self._user_sessions.get(user_id)

    @property
    def strategy_order_quantity(self) -> Decimal:
        """Return the process-local quantity used for new strategy entries."""
        return self._strategy_order_quantity

    async def set_strategy_order_quantity(self, quantity: Decimal) -> Decimal:
        """Update automatic entry sizing for subsequent signals only."""
        if not quantity.is_finite() or quantity <= 0:
            raise BinanceOrderError("strategy order quantity must be greater than zero", 422)
        async with self._lock:
            self._strategy_order_quantity = quantity
            return self._strategy_order_quantity

    async def connect_credentials(self, api_key: str, api_secret: str) -> dict[str, Any]:
        """Validate and retain credentials only for this backend process."""
        if not api_key.strip() or not api_secret.strip():
            raise BinanceOrderError("API key and secret are required", 422)
        previous_key, previous_secret = self._runtime_api_key, self._runtime_api_secret
        self._runtime_api_key = SecretStr(api_key.strip())
        self._runtime_api_secret = SecretStr(api_secret.strip())
        try:
            account = await self.account()
        except Exception:
            self._runtime_api_key, self._runtime_api_secret = previous_key, previous_secret
            raise
        if not account.get("canTrade"):
            self._runtime_api_key, self._runtime_api_secret = previous_key, previous_secret
            raise BinanceOrderError("these Binance Demo credentials are not permitted to trade", 403)
        self._session_execution_enabled = True
        return {
            "account_type": account.get("accountType"),
            "can_trade": bool(account.get("canTrade")),
        }

    def clear_credentials(self) -> None:
        self._runtime_api_key = None
        self._runtime_api_secret = None
        self._session_execution_enabled = False

    async def connect_user_credentials(self, user_id: int, api_key: str, api_secret: str) -> dict[str, Any]:
        """Activate one account's in-memory session and verify it against Binance.

        Persisting the encrypted keys is the caller's job; this only keeps the
        decrypted values for the lifetime of the backend process. A failed
        verification (bad keys or Binance unreachable) is reported in the result
        instead of raised so sign-up and sign-in still complete.
        """
        if not api_key.strip() or not api_secret.strip():
            raise BinanceOrderError("API key and secret are required", 422)
        self._user_sessions[user_id] = _UserSession(SecretStr(api_key.strip()), SecretStr(api_secret.strip()))
        if user_id not in self._strategy_enabled_users:
            self._strategy_enabled_users[user_id] = self._repository.get_user_strategy_enabled(user_id) if hasattr(self._repository, "get_user_strategy_enabled") else True
        token = current_user_id.set(user_id)
        try:
            try:
                account = await self.account()
                can_trade = bool(account.get("canTrade"))
            except BinanceOrderError as error:
                return {"connected": False, "verified": False, "can_trade": False, "error": str(error)}
            session = self._session(user_id)
            if session is None:
                return {"connected": False, "verified": False, "can_trade": False, "error": "session was removed during verification"}
            session.verified = True
            session.enabled = can_trade
            if not can_trade:
                return {"connected": True, "verified": True, "can_trade": False, "error": "these Binance Demo credentials are not permitted to trade"}
            self.create_background_task(
                self._reconcile_account_reservations(user_id),
                name=f"reservation-reconcile-user-{user_id}",
            )
            return {"connected": True, "verified": True, "can_trade": True, "error": None}
        finally:
            current_user_id.reset(token)

    def deactivate_user_session(self, user_id: int) -> None:
        """Drop the in-memory decrypted keys for an account (logout)."""
        self._user_sessions.pop(user_id, None)

    def has_active_session(self, user_id: int) -> bool:
        return user_id in self._user_sessions

    def active_strategy_user_ids(self) -> tuple[int, ...]:
        """Users whose verified Demo sessions can receive automatic orders."""
        return tuple(user_id for user_id, session in self._user_sessions.items() if session.verified and session.enabled and self._strategy_enabled_users.get(user_id, True))

    def active_execution_user_ids(self) -> tuple[int, ...]:
        """All verified accounts, including those with automatic strategy disabled."""
        return tuple(user_id for user_id, session in self._user_sessions.items() if session.verified)

    def strategy_status(self, user_id: int) -> dict[str, Any]:
        return {"enabled": self._strategy_enabled_users.get(user_id, True), "last_event": self._strategy_status.get(user_id)}

    def set_strategy_enabled(self, user_id: int, enabled: bool) -> dict[str, Any]:
        self._strategy_enabled_users[user_id] = enabled
        if hasattr(self._repository, "set_user_strategy_enabled"):
            self._repository.set_user_strategy_enabled(user_id, enabled)
        return self.strategy_status(user_id)

    def _user_execution_enabled(self, user_id: int | None) -> bool:
        if user_id is None:
            return self.execution_enabled
        session = self._user_sessions.get(user_id)
        return bool(session and session.verified and session.enabled)

    async def process_signal_for_user(self, user_id: int, signal: Signal) -> None:
        """Execute one background strategy signal with the user's credentials."""
        token = current_user_id.set(user_id)
        try:
            self._strategy_status[user_id] = {"state": "received", "symbol": signal.symbol, "variant": signal.variant.value, "action": signal.action.value, "timestamp": signal.timestamp.isoformat(), "error": None}
            await self.process_signal(signal)
        finally:
            current_user_id.reset(token)

    async def order_log(self, symbol: str | None = None, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Audit rows for the bound user; falls back to legacy history otherwise."""
        user_id = current_user_id.get()
        if user_id is not None and hasattr(self._repository, "get_user_orders"):
            return tuple(await asyncio.to_thread(self._repository.get_user_orders, user_id, symbol, limit))
        return await self.orders(symbol, limit)

    async def positions(self, symbol: str | None = None) -> tuple[Position, ...]:
        user_id = current_user_id.get()
        async with self._lock:
            return tuple(p.model_copy(deep=True) for (owner, item_symbol, _), p in self._positions.items() if owner == user_id and (symbol is None or item_symbol == symbol))

    async def position_history(self, symbol: str | None = None, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Closed strategy positions for the bound account, newest first."""
        user_id = current_user_id.get()
        if user_id is None or not hasattr(self._repository, "get_position_history"):
            return ()
        return tuple(await asyncio.to_thread(self._repository.get_position_history, user_id, symbol, limit))

    async def orders(self, symbol: str | None = None, limit: int = 100) -> tuple[dict[str, Any], ...]:
        async with self._lock:
            selected = [item.copy() for item in self._history if symbol is None or item.get("symbol") == symbol]
            return tuple(selected[-limit:])

    async def process_signal(self, signal: Signal) -> None:
        if not self.execution_enabled:
            return
        key: PositionKey = (current_user_id.get(), signal.symbol, signal.variant)
        async with self._lock:
            position = self._positions.get(key)
            if key in self._inflight:
                return
            if signal.action is SignalAction.BUY and position and position.status is PositionStatus.OPEN:
                return
            if signal.action is SignalAction.EXIT and (not position or position.status is not PositionStatus.OPEN):
                return
            if signal.action is SignalAction.BUY:
                owner = key[0]
                open_count = sum(
                    1 for (position_owner, _, _), item in self._positions.items()
                    if position_owner == owner and item.status is PositionStatus.OPEN
                )
                pending_count = sum(1 for pending in self._pending_entries if pending[0] == owner)
                if open_count + pending_count >= self._settings.max_open_positions_per_user:
                    return
                self._pending_entries.add(key)
            self._inflight.add(key)
        try:
            if signal.action is SignalAction.BUY:
                await self._enter(signal)
            else:
                await self._exit(key, signal.price, signal.timestamp, ExitReason.SIGNAL)
        except BinanceOrderError as error:
            user_id = current_user_id.get()
            if user_id is not None:
                self._strategy_status[user_id] = {**self._strategy_status.get(user_id, {}), "state": "failed", "error": str(error), "updated_at": datetime.now(UTC).isoformat()}
            logger.exception("strategy_order_failed symbol=%s variant=%s", signal.symbol, signal.variant)
        else:
            user_id = current_user_id.get()
            if user_id is not None:
                self._strategy_status[user_id] = {**self._strategy_status.get(user_id, {}), "state": "processed", "error": None, "updated_at": datetime.now(UTC).isoformat()}
        finally:
            async with self._lock:
                self._inflight.discard(key)
                self._pending_entries.discard(key)

    async def process_tick(self, tick: Tick) -> None:
        exits: list[tuple[PositionKey, ExitReason]] = []
        async with self._lock:
            for key in tuple(self._positions_by_symbol.get(tick.symbol, ())):
                position = self._positions.get(key)
                owner_id, symbol, _ = key
                if symbol != tick.symbol:
                    continue
                if not position or position.status is not PositionStatus.OPEN:
                    continue
                position.current_price = tick.price
                position.current_pnl = (tick.price - position.entry_price) * position.quantity
                if self._user_execution_enabled(owner_id) and key not in self._inflight:
                    reason = ExitReason.STOP_LOSS if tick.price <= position.stop_loss_price else ExitReason.TAKE_PROFIT if tick.price >= position.take_profit_price else None
                    if reason:
                        self._inflight.add(key)
                        exits.append((key, reason))
        for key, reason in exits:
            self.create_background_task(
                self._execute_risk_exit(key, tick.price, tick.timestamp, reason),
                name=f"risk-exit-{key[0]}-{key[1]}-{key[2].value}",
            )

    async def _execute_risk_exit(self, key: PositionKey, price: Decimal, timestamp: datetime, reason: ExitReason) -> None:
        token = current_user_id.set(key[0])
        try:
            await self._exit(key, price, timestamp, reason)
        except BinanceOrderError:
            logger.exception("risk_exit_failed user=%s symbol=%s variant=%s", *key)
        finally:
            current_user_id.reset(token)
            async with self._lock:
                self._inflight.discard(key)

    async def place_order(self, params: dict[str, Any], test: bool = False) -> dict[str, Any]:
        if not test and not self.execution_enabled:
            raise BinanceOrderError("order execution is disabled by the global kill switch", 409)
        if not test and self._consecutive_order_failures >= self._settings.max_consecutive_order_failures:
            if monotonic() < self._circuit_open_until:
                raise BinanceOrderError("order circuit breaker is open after consecutive exchange failures", 503)
            self._consecutive_order_failures = 0
        clean = params.copy()
        audit = clean.pop("_audit", {})
        normalized = await self._normalize_order(clean)
        rules = await self._rules(normalized["symbol"])
        await self._enforce_min_notional(normalized, rules)
        reservation = None
        if not test:
            try:
                await self._ensure_market_fresh(normalized)
                reservation = await self._risk.reserve(current_user_id.get(), normalized, rules, self.account, self._market_price)
            except PreTradeRiskError as error:
                self._metric_counts["risk_rejections"] += 1
                raise BinanceOrderError(str(error), 422) from error
        try:
            response = await self._signed_request("POST", "/api/v3/order/test" if test else "/api/v3/order", normalized)
        except BinanceOrderError as error:
            if not test and error.uncertain:
                await self._risk.mark_uncertain(reservation)
                reconciled = await self._reconciliation.reconcile_unknown(normalized, self._query_by_client_id)
                self._metric_counts["reconciliations"] += 1
                if reconciled is None:
                    raise BinanceOrderError("order outcome remains unknown after reconciliation; capital reservation retained", 503, uncertain=True) from error
                response = reconciled
                await self._risk.acknowledge(reservation, str(response.get("status") or "UNKNOWN"))
            else:
                await self._risk.release(reservation)
                self._consecutive_order_failures += 1
                if self._consecutive_order_failures >= self._settings.max_consecutive_order_failures:
                    self._circuit_open_until = monotonic() + self._settings.order_circuit_breaker_cooldown_seconds
                raise
        else:
            await self._risk.acknowledge(reservation, str(response.get("status") or "UNKNOWN"))
        if not test:
            self._consecutive_order_failures = 0
            self._circuit_open_until = 0.0
            await self._record(response, normalized, audit if isinstance(audit, dict) else {})
        return response

    async def _ensure_market_fresh(self, order: dict[str, str]) -> None:
        """Reject only risk-increasing orders when streamed market data is stale."""
        if order.get("side") != "BUY" or self._market_tick_loader is None:
            return
        tick = await self._market_tick_loader(order["symbol"])
        if tick is None:
            raise PreTradeRiskError("risk guard rejected order: no live market tick is available")
        age = (datetime.now(UTC) - tick.timestamp).total_seconds()
        if age > self._settings.max_market_data_age_seconds:
            raise PreTradeRiskError(
                f"risk guard rejected order: market data is stale ({age:.2f}s old)"
            )
        if "price" in order:
            price = Decimal(order["price"])
            deviation = abs(price - tick.price) / tick.price * Decimal(100)
            if deviation > Decimal(str(self._settings.max_price_deviation_percent)):
                raise PreTradeRiskError(
                    f"risk guard rejected order: price deviation {deviation:.2f}% exceeds maximum"
                )

    async def _enforce_min_notional(self, order: dict[str, str], rules: dict[str, Any]) -> None:
        """Reject orders valued below the symbol minimum with an actionable message.

        MARKET orders carry no price, so Binance's NOTIONAL filter rejects them
        with an opaque "Filter failure: NOTIONAL" (typically a dust remainder
        being squared off). Price-bearing orders are already checked during
        normalization; everything else is valued at the live market price here.
        """
        if "price" in order and "quantity" in order:
            return
        notional_filter = rules.get("NOTIONAL") or rules.get("MIN_NOTIONAL") or {}
        minimum = Decimal(str(notional_filter.get("minNotional", "0")))
        if minimum <= 0:
            return
        if "quoteOrderQty" in order:
            notional = Decimal(order["quoteOrderQty"])
        elif "quantity" in order:
            notional = Decimal(order["quantity"]) * await self._market_price(order["symbol"])
        else:
            return
        if notional < minimum:
            quote = str(rules.get("_quoteAsset") or "USDT")
            raise BinanceOrderError(
                f"order value {format(notional.normalize(), 'f')} {quote} is below Binance's "
                f"{format(minimum.normalize(), 'f')} {quote} minimum",
                422,
            )

    async def _check_balance_utilization(self, order: dict[str, str]) -> None:
        """Compatibility wrapper used by focused risk tests."""
        try:
            reservation = await self._risk.reserve(current_user_id.get(), order, await self._rules(order["symbol"]), self.account, self._market_price)
        except PreTradeRiskError as error:
            raise BinanceOrderError(str(error), 422) from error
        await self._risk.release(reservation)

    async def _market_price(self, symbol: str) -> Decimal:
        ticker = await self._public_request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return Decimal(str(ticker.get("price", "0")))

    async def _query_by_client_id(self, symbol: str, client_id: str) -> dict[str, Any]:
        result = await self._signed_request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id})
        return result if isinstance(result, dict) else {}

    async def _reconcile_account_reservations(self, user_id: int) -> None:
        token = current_user_id.set(user_id)
        try:
            for reservation in self._risk.pending(user_id):
                if not reservation.client_order_id:
                    continue
                try:
                    result = await self._query_by_client_id(reservation.symbol, reservation.client_order_id)
                except BinanceOrderError:
                    logger.warning("reservation_reconciliation_deferred user=%s client_id=%s", user_id, reservation.client_order_id)
                    continue
                await self._risk.acknowledge(reservation, str(result.get("status") or "UNKNOWN"))
        finally:
            current_user_id.reset(token)

    async def _reconcile_strategy_positions(self, user_id: int | None) -> None:
        """Close open positions the strategy is already flat on.

        The strategy emits only on SMA/EMA relation *changes* and seeding after a
        restart deliberately suppresses signals, so a crossover that happened
        while the process was down (or while nobody was signed in) leaves a
        position open with nothing left to trigger an exit. This closes any open
        position whose symbol is no longer in an uptrend, using the owner's keys.
        """
        if self._strategy_relation_loader is None or not self._user_execution_enabled(user_id):
            return
        async with self._lock:
            candidates = [
                key
                for key, position in self._positions.items()
                if key[0] == user_id
                and position.status is PositionStatus.OPEN
                and key not in self._inflight
                and key not in self._reconciled_positions
            ]
        for key in candidates:
            relation = await self._strategy_relation_loader(key[1])
            if relation is None or relation > 0:
                continue
            token = current_user_id.set(key[0])
            try:
                async with self._lock:
                    position = self._positions.get(key)
                    if (
                        position is None
                        or position.status is not PositionStatus.OPEN
                        or key in self._inflight
                    ):
                        continue
                    self._inflight.add(key)
                    self._reconciled_positions.add(key)
                    price = position.current_price
                await self._exit(key, price, datetime.now(UTC), ExitReason.SIGNAL)
                logger.info("strategy_position_reconciled user=%s symbol=%s variant=%s", *key)
            except BinanceOrderError:
                logger.warning("strategy_position_reconcile_deferred user=%s symbol=%s variant=%s", *key)
            finally:
                current_user_id.reset(token)
                async with self._lock:
                    self._inflight.discard(key)

    async def query_order(self, symbol: str, order_id: int | None, client_id: str | None) -> dict[str, Any]:
        params = self._order_reference(symbol, order_id, client_id)
        response = await self._signed_request("GET", "/api/v3/order", params)
        await self._risk.acknowledge_client(current_user_id.get(), str(response.get("clientOrderId") or client_id or ""), str(response.get("status") or "UNKNOWN"))
        await self._record(response, params)
        return response

    async def cancel_order(self, symbol: str, order_id: int | None, client_id: str | None) -> dict[str, Any]:
        params = self._order_reference(symbol, order_id, client_id)
        response = await self._signed_request("DELETE", "/api/v3/order", params)
        await self._risk.acknowledge_client(current_user_id.get(), str(response.get("clientOrderId") or client_id or ""), str(response.get("status") or "CANCELED"))
        await self._record(response, params)
        return response

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        response = await self._signed_request("GET", "/api/v3/openOrders", {} if symbol is None else {"symbol": symbol})
        return response if isinstance(response, list) else []

    async def account_orders(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        response = await self._signed_request("GET", "/api/v3/allOrders", {"symbol": symbol, "limit": limit})
        return response if isinstance(response, list) else []

    async def place_order_list(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.execution_enabled:
            raise BinanceOrderError("order execution is disabled by the global kill switch", 409)
        normalized_kind = kind.upper()
        path = self.ORDER_LIST_PATHS.get(normalized_kind)
        if path is None:
            raise BinanceOrderError("order-list type must be OCO, OTO, OTOCO, OPO, or OPOCO", 422)
        clean = {key: value for key, value in params.items() if key not in {"signature", "timestamp", "recvWindow"} and value is not None}
        symbol = str(clean.get("symbol", "")).strip().upper()
        if not symbol.isalnum():
            raise BinanceOrderError("a valid symbol is required", 422)
        clean["symbol"] = symbol
        response = await self._signed_request("POST", path, clean)
        for report in response.get("orderReports", []):
            if isinstance(report, dict):
                await self._record(report, {"symbol": symbol, "orderListType": normalized_kind})
        return response

    async def query_order_list(self, order_list_id: int | None, client_id: str | None) -> dict[str, Any]:
        if order_list_id is None and not client_id:
            raise BinanceOrderError("orderListId or origClientOrderId is required", 422)
        params = {"orderListId": order_list_id} if order_list_id is not None else {"origClientOrderId": client_id}
        return await self._signed_request("GET", "/api/v3/orderList", params)

    async def cancel_order_list(self, symbol: str, order_list_id: int | None, client_id: str | None) -> dict[str, Any]:
        if order_list_id is None and not client_id:
            raise BinanceOrderError("orderListId or listClientOrderId is required", 422)
        params: dict[str, Any] = {"symbol": symbol}
        params.update({"orderListId": order_list_id} if order_list_id is not None else {"listClientOrderId": client_id})
        return await self._signed_request("DELETE", "/api/v3/orderList", params)

    async def order_lists(self, open_only: bool = True) -> list[dict[str, Any]]:
        path = "/api/v3/openOrderList" if open_only else "/api/v3/allOrderList"
        response = await self._signed_request("GET", path, {})
        return response if isinstance(response, list) else []

    async def cancel_all(self, symbol: str) -> list[dict[str, Any]]:
        response = await self._signed_request("DELETE", "/api/v3/openOrders", {"symbol": symbol})
        for item in response if isinstance(response, list) else []:
            if isinstance(item, dict):
                await self._record(item, {"symbol": symbol})
        return response if isinstance(response, list) else []

    async def account(self) -> dict[str, Any]:
        response = await self._signed_request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        return response if isinstance(response, dict) else {}

    async def pnl(self, symbol: str, current_price: Decimal) -> dict[str, Any]:
        """Build FIFO realized/unrealized P&L from authoritative Binance fills."""
        trades = await self._all_trades(symbol)
        rules = await self._rules(symbol)
        base_asset = rules.get("_baseAsset", symbol.removesuffix("USDT"))
        quote_asset = rules.get("_quoteAsset", "USDT")
        notional_filter = rules.get("NOTIONAL") or rules.get("MIN_NOTIONAL") or {}
        min_notional = Decimal(str(notional_filter.get("minNotional", "0")))
        lots: deque[dict[str, Any]] = deque()
        realized_by_order: dict[int, Decimal] = {}
        fees_other: dict[str, Decimal] = {}
        for trade in sorted(trades, key=lambda item: (int(item.get("time", 0)), int(item.get("id", 0)))):
            quantity = Decimal(str(trade["qty"]))
            quote = Decimal(str(trade["quoteQty"]))
            commission = Decimal(str(trade.get("commission", "0")))
            commission_asset = str(trade.get("commissionAsset", ""))
            order_id = int(trade["orderId"])
            if commission_asset not in {base_asset, quote_asset} and commission:
                fees_other[commission_asset] = fees_other.get(commission_asset, Decimal(0)) + commission
            if trade.get("isBuyer"):
                net_quantity = quantity - commission if commission_asset == base_asset else quantity
                cost = quote + commission if commission_asset == quote_asset else quote
                if net_quantity > 0:
                    lots.append({"order_id": order_id, "quantity": net_quantity, "cost": cost})
                continue
            disposed = quantity + commission if commission_asset == base_asset else quantity
            proceeds = quote - commission if commission_asset == quote_asset else quote
            remaining = disposed
            matched_cost = Decimal(0)
            matched_quantity = Decimal(0)
            while remaining > 0 and lots:
                lot = lots[0]
                used = min(remaining, lot["quantity"])
                unit_cost = lot["cost"] / lot["quantity"]
                matched_cost += unit_cost * used
                matched_quantity += used
                lot["quantity"] -= used
                lot["cost"] -= unit_cost * used
                remaining -= used
                if lot["quantity"] <= 0:
                    lots.popleft()
            if matched_quantity > 0:
                matched_proceeds = proceeds * (matched_quantity / disposed)
                realized_by_order[order_id] = realized_by_order.get(order_id, Decimal(0)) + matched_proceeds - matched_cost

        unrealized_by_order: dict[int, Decimal] = {}
        remaining_by_order: dict[int, Decimal] = {}
        open_quantity = Decimal(0)
        open_cost = Decimal(0)
        for lot in lots:
            open_quantity += lot["quantity"]
            open_cost += lot["cost"]
            unrealized_by_order[lot["order_id"]] = unrealized_by_order.get(lot["order_id"], Decimal(0)) + current_price * lot["quantity"] - lot["cost"]
            remaining_by_order[lot["order_id"]] = remaining_by_order.get(lot["order_id"], Decimal(0)) + lot["quantity"]
        order_ids = set(realized_by_order) | set(unrealized_by_order)
        return {
            "symbol": symbol,
            "method": "FIFO",
            "current_price": current_price,
            "quote_asset": quote_asset,
            "min_notional": min_notional,
            "open_quantity": open_quantity,
            "open_cost": open_cost,
            "market_value": open_quantity * current_price,
            "realized_pnl": sum(realized_by_order.values(), Decimal(0)),
            "unrealized_pnl": open_quantity * current_price - open_cost,
            "total_pnl": sum(realized_by_order.values(), Decimal(0)) + open_quantity * current_price - open_cost,
            "orders": [
                {"order_id": order_id, "realized_pnl": realized_by_order.get(order_id), "unrealized_pnl": unrealized_by_order.get(order_id), "remaining_quantity": remaining_by_order.get(order_id, Decimal(0)), "notional": remaining_by_order.get(order_id, Decimal(0)) * current_price}
                for order_id in sorted(order_ids)
            ],
            "unconverted_commissions": fees_other,
        }

    async def _all_trades(self, symbol: str) -> list[dict[str, Any]]:
        """Page the complete fill history without the former 1,000-fill truncation."""
        collected: list[dict[str, Any]] = []
        from_id: int | None = None
        while True:
            params: dict[str, Any] = {"symbol": symbol, "limit": 1000}
            if from_id is not None:
                params["fromId"] = from_id
            page = await self._signed_request("GET", "/api/v3/myTrades", params)
            if not isinstance(page, list) or not page:
                break
            collected.extend(page)
            if len(page) < 1000:
                break
            next_id = int(page[-1]["id"]) + 1
            if from_id is not None and next_id <= from_id:
                break
            from_id = next_id
        return collected

    async def square_off_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        """Market-sell the quantity still remaining on one filled buy order.

        Whether a lot is already squared off is decided by Binance's own fill
        ledger (via FIFO), never by remembered client state, so a partial or
        previously-failed attempt can be retried and the answer always matches
        the remaining quantity the dashboard displays.
        """
        key = (symbol, order_id)
        async with self._lock:
            if key in self._squaring_orders:
                raise BinanceOrderError("a square-off for this order is already in flight", 409)
            self._squaring_orders.add(key)
        try:
            pnl_state = await self.pnl(symbol, Decimal(0))
            pnl_order = next((item for item in pnl_state["orders"] if item["order_id"] == order_id), None)
            remaining_quantity = Decimal(0) if pnl_order is None else Decimal(pnl_order["remaining_quantity"])
            if remaining_quantity <= 0:
                raise BinanceOrderError("this buy order has already been fully squared off", 409)
            async with self._lock:
                matches = [item for item in self._history if item.get("symbol") == symbol and int(item.get("orderId", -1)) == order_id]
                source = matches[-1] if matches else None
            if source is None:
                source = await self.query_order(symbol, order_id, None)
            if not source or source.get("side") != "BUY" or source.get("status") != "FILLED":
                raise BinanceOrderError("only a filled buy order can be squared off", 422)
            sell_quantity = await self._sellable_quantity(symbol, remaining_quantity)
            return await self.place_order({
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(sell_quantity),
                "newClientOrderId": f"ctsSQ{order_id}"[:36],
                "newOrderRespType": "FULL",
            })
        finally:
            async with self._lock:
                self._squaring_orders.discard(key)

    async def square_off(self, symbol: str, variant: StrategyVariant) -> None:
        if not self.execution_enabled:
            raise BinanceOrderError("order execution is disabled by the global kill switch", 409)
        key: PositionKey = (current_user_id.get(), symbol, variant)
        async with self._lock:
            position = self._positions.get(key)
            if not position or position.status is not PositionStatus.OPEN:
                raise BinanceOrderError("no open position exists for this symbol and variant", 404)
            if key in self._inflight:
                raise BinanceOrderError("a position order is already in flight", 409)
            self._inflight.add(key)
            price = position.current_price
        try:
            await self._exit(key, price, datetime.now(UTC), ExitReason.MANUAL)
        finally:
            async with self._lock:
                self._inflight.discard(key)

    async def _enter(self, signal: Signal) -> None:
        async with self._lock:
            quantity = self._strategy_order_quantity
        response = await self.place_order({"symbol": signal.symbol, "side": "BUY", "type": "MARKET", "quantity": str(quantity), "newClientOrderId": self._client_id(signal, "B"), "newOrderRespType": "FULL", "_audit": {"source": "strategy", "strategy_variant": signal.variant.value, "signal_reason": signal.reason, "signal_action": signal.action.value}})
        quantity, price = self._execution(response, signal.price)
        if quantity <= 0:
            return
        stop = self._settings.variant_a_stop_loss_percent if signal.variant is StrategyVariant.A else self._settings.variant_b_stop_loss_percent
        position = Position(symbol=signal.symbol, variant=signal.variant, status=PositionStatus.OPEN, quantity=quantity, entry_price=price, current_price=price, current_pnl=Decimal(0), stop_loss_price=price * (Decimal(1) - Decimal(str(stop)) / 100), take_profit_price=price * (Decimal(1) + Decimal(str(self._settings.take_profit_percent)) / 100), opened_at=signal.timestamp)
        async with self._lock:
            user_id = current_user_id.get()
            self._positions[(user_id, signal.symbol, signal.variant)] = position
            self._positions_by_symbol.setdefault(signal.symbol, set()).add(
                (user_id, signal.symbol, signal.variant)
            )
            self._reconciled_positions.discard((user_id, signal.symbol, signal.variant))
        if user_id is not None and hasattr(self._repository, "save_user_position"):
            await asyncio.to_thread(self._repository.save_user_position, user_id, position)
        else:
            await asyncio.to_thread(self._repository.save_position, position)

    async def _exit(self, key: PositionKey, fallback: Decimal, timestamp: datetime, reason: ExitReason) -> None:
        async with self._lock:
            position = self._positions.get(key)
            if not position or position.status is not PositionStatus.OPEN:
                return
            quantity = position.quantity
        _, symbol, variant = key
        sell_quantity = await self._sellable_quantity(symbol, quantity)
        response = await self.place_order({"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": str(sell_quantity), "newClientOrderId": f"cts{variant.value}{symbol}S{int(timestamp.timestamp())}"[:36], "newOrderRespType": "FULL", "_audit": {"source": "strategy", "strategy_variant": variant.value, "exit_reason": reason.value}})
        executed, price = self._execution(response, fallback)
        if executed <= 0:
            return
        async with self._lock:
            position = self._positions.get(key)
            if position:
                remaining = max(Decimal(0), position.quantity - executed)
                updated = position.model_dump()
                updated.update(
                    current_price=price,
                    current_pnl=(price - position.entry_price) * executed,
                    quantity=remaining if remaining > 0 else position.quantity,
                    status=PositionStatus.OPEN if remaining > 0 else PositionStatus.CLOSED,
                    closed_at=None if remaining > 0 else timestamp,
                )
                self._positions[key] = Position.model_validate(updated)
                saved_position = self._positions[key]
                if self._history:
                    self._history[-1].update(exit_reason=reason.value, strategy_variant=variant.value)
            else:
                saved_position = None
        if saved_position is not None:
            if key[0] is not None and hasattr(self._repository, "save_user_position"):
                await asyncio.to_thread(self._repository.save_user_position, key[0], saved_position)
            else:
                await asyncio.to_thread(self._repository.save_position, saved_position)
        if (
            saved_position is not None
            and saved_position.status is PositionStatus.CLOSED
            and key[0] is not None
            and hasattr(self._repository, "record_position_history")
        ):
            await asyncio.to_thread(self._repository.record_position_history, key[0], saved_position, reason.value)

    async def _sellable_quantity(self, symbol: str, requested: Decimal) -> Decimal:
        """Return the spendable base quantity accepted by Binance market filters.

        A filled BUY's ``executedQty`` is gross. When commission is charged in
        the base asset, the account's free balance is slightly lower. Capping
        against the authoritative balance prevents insufficient-balance errors;
        flooring to MARKET_LOT_SIZE prevents precision/step-size rejections.
        """
        rules = await self._rules(symbol)
        base_asset = str(rules.get("_baseAsset") or symbol.removesuffix("USDT"))
        account = await self.account()
        balance = next(
            (item for item in account.get("balances", []) if item.get("asset") == base_asset),
            None,
        )
        free = Decimal(str(balance.get("free", "0"))) if balance else Decimal(0)
        available = min(requested, free)
        market_lot = rules.get("MARKET_LOT_SIZE", {})
        lot = market_lot if market_lot.get("stepSize") not in {None, "0.00000000"} else rules.get("LOT_SIZE", {})
        normalized = Decimal(self._floor(str(available), lot.get("stepSize")))
        minimum = Decimal(str(lot.get("minQty", "0")))
        if normalized <= 0 or normalized < minimum:
            raise BinanceOrderError(
                f"available {base_asset} balance ({free}) is below the minimum sell quantity",
                422,
            )
        return normalized

    async def _normalize_order(self, params: dict[str, Any]) -> dict[str, str]:
        result = {key: str(value) for key, value in params.items() if value is not None}
        result["symbol"] = result.get("symbol", "").strip().upper()
        result["side"] = result.get("side", "").upper()
        result["type"] = result.get("type", "").upper()
        if not result["symbol"].isalnum() or result["side"] not in {"BUY", "SELL"} or result["type"] not in self.ORDER_TYPES:
            raise BinanceOrderError("invalid symbol, side, or Spot order type", 422)
        self._validate_required(result)
        rules = await self._rules(result["symbol"])
        market_lot = rules.get("MARKET_LOT_SIZE", {})
        quantity_filter = (
            "MARKET_LOT_SIZE"
            if result["type"] == "MARKET" and market_lot.get("stepSize") not in {None, "0.00000000"}
            else "LOT_SIZE"
        )
        for field, filter_name in (("quantity", quantity_filter), ("icebergQty", "LOT_SIZE")):
            if field in result:
                result[field] = self._floor(result[field], rules.get(filter_name, {}).get("stepSize"))
        for field in ("price", "stopPrice"):
            if field in result:
                result[field] = self._floor(result[field], rules.get("PRICE_FILTER", {}).get("tickSize"))
        self._validate_filters(result, rules)
        return result

    def _validate_required(self, params: dict[str, str]) -> None:
        order_type = params["type"]
        required = {"LIMIT": {"timeInForce", "quantity", "price"}, "STOP_LOSS_LIMIT": {"timeInForce", "quantity", "price"}, "TAKE_PROFIT_LIMIT": {"timeInForce", "quantity", "price"}, "LIMIT_MAKER": {"quantity", "price"}, "STOP_LOSS": {"quantity"}, "TAKE_PROFIT": {"quantity"}}.get(order_type, set())
        missing = required - params.keys()
        if missing:
            raise BinanceOrderError(f"missing required parameters: {', '.join(sorted(missing))}", 422)
        if order_type == "MARKET" and ("quantity" in params) == ("quoteOrderQty" in params):
            raise BinanceOrderError("MARKET requires exactly one of quantity or quoteOrderQty", 422)
        if order_type in {"STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"} and "stopPrice" not in params and "trailingDelta" not in params:
            raise BinanceOrderError("stop order requires stopPrice or trailingDelta", 422)
        if "timeInForce" in params and params["timeInForce"] not in self.TIME_IN_FORCE:
            raise BinanceOrderError("timeInForce must be GTC, IOC, or FOK", 422)
        if "icebergQty" in params and params.get("timeInForce") != "GTC":
            raise BinanceOrderError("iceberg orders require GTC", 422)

    def _validate_filters(self, params: dict[str, str], rules: dict[str, Any]) -> None:
        if "quantity" in params:
            market_lot = rules.get("MARKET_LOT_SIZE", {})
            lot = (
                market_lot
                if params.get("type") == "MARKET" and market_lot.get("stepSize") not in {None, "0.00000000"}
                else rules.get("LOT_SIZE", {})
            )
            quantity = Decimal(params["quantity"])
            if quantity <= 0 or quantity < Decimal(lot.get("minQty", "0")):
                raise BinanceOrderError("quantity is below the symbol minimum", 422)
            maximum = Decimal(lot.get("maxQty", "0"))
            if maximum and quantity > maximum:
                raise BinanceOrderError("quantity exceeds the symbol maximum", 422)
        if "price" in params:
            price, rule = Decimal(params["price"]), rules.get("PRICE_FILTER", {})
            if price <= 0 or price < Decimal(rule.get("minPrice", "0")):
                raise BinanceOrderError("price is below the symbol minimum", 422)
        notional = rules.get("NOTIONAL") or rules.get("MIN_NOTIONAL") or {}
        if "quantity" in params and "price" in params and Decimal(params["quantity"]) * Decimal(params["price"]) < Decimal(notional.get("minNotional", "0")):
            raise BinanceOrderError("order notional is below the symbol minimum", 422)

    async def _rules(self, symbol: str) -> dict[str, Any]:
        loaded_at = self._exchange_info_loaded_at.get(symbol, 0.0)
        if monotonic() - loaded_at >= self._settings.exchange_info_cache_seconds:
            payload = await self._public_request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
            items = payload.get("symbols", [])
            if items:
                item = items[0]
                self._symbol_rules[symbol] = {
                    entry["filterType"]: entry for entry in item.get("filters", [])
                }
                self._symbol_rules[symbol]["_baseAsset"] = item.get("baseAsset")
                self._symbol_rules[symbol]["_quoteAsset"] = item.get("quoteAsset")
                self._exchange_info_loaded_at[symbol] = monotonic()
        if symbol not in self._symbol_rules:
            raise BinanceOrderError("symbol is not available on Binance Demo Mode", 422)
        return self._symbol_rules[symbol]

    async def _record(self, response: dict[str, Any], request_params: dict[str, Any], audit: dict[str, Any] | None = None) -> None:
        record = {**request_params, **(audit or {}), **response, "recordedAt": datetime.now(UTC).isoformat()}
        user_id = current_user_id.get()
        if user_id is not None:
            # A signed-in account: persist a queryable audit row (status, price,
            # quantity, timestamps) without touching the process-global history.
            if hasattr(self._repository, "log_user_order"):
                await asyncio.to_thread(self._repository.log_user_order, user_id, record, str(record.get("source") or "manual"))
            return
        client_id = str(record.get("clientOrderId") or record.get("newClientOrderId") or "")
        async with self._lock:
            if client_id:
                self._orders[client_id] = record
            self._history.append(record)
        await asyncio.to_thread(self._repository.save_order, record)

    async def _signed_request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        api_key, secret = self._api_key(), self._api_secret()
        if not api_key or not secret:
            raise BinanceOrderError("Binance Demo API credentials are not configured", 503)
        signed = {**params, "recvWindow": self._settings.order_recv_window_milliseconds, "timestamp": int(time() * 1000)}
        query = urlencode(signed)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return await self._request(method, path, f"{query}&signature={signature}", api_key)

    async def _public_request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        return await self._request(method, path, urlencode(params), None)

    async def _request(self, method: str, path: str, query: str, api_key: str | None) -> Any:
        started = monotonic()
        self._metric_counts["requests"] += 1
        try:
            try:
                async with self._request_semaphore:
                    return await asyncio.wait_for(
                        asyncio.to_thread(self._request_sync, method, path, query, api_key),
                        timeout=self._settings.order_request_timeout_seconds + 1,
                    )
            except TimeoutError as error:
                raise BinanceOrderError(
                    "Binance did not respond in time; reconciling by client order ID",
                    uncertain=True,
                ) from error
        except Exception:
            self._metric_counts["errors"] += 1
            raise
        finally:
            self._request_latencies_ms.append((monotonic() - started) * 1000)

    def metrics(self) -> dict[str, Any]:
        values = sorted(self._request_latencies_ms)
        percentile = lambda fraction: values[min(len(values) - 1, int((len(values) - 1) * fraction))] if values else 0.0
        return {
            **self._metric_counts,
            "active_tasks": len(self._background_tasks),
            "inflight_positions": len(self._inflight),
            "circuit_breaker_open": self._consecutive_order_failures >= self._settings.max_consecutive_order_failures,
            "latency_ms": {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)},
        }

    def _request_sync(self, method: str, path: str, query: str, api_key: str | None) -> Any:
        url = f"{self._settings.binance_testnet_rest_url}{path}"
        data = query.encode() if method in {"POST", "PUT"} else None
        if method not in {"POST", "PUT"} and query:
            url = f"{url}?{query}"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if api_key:
            headers["X-MBX-APIKEY"] = api_key
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=self._settings.order_request_timeout_seconds) as response:  # noqa: S310
                body = response.read()
                return json.loads(body) if body else {}
        except HTTPError as error:
            try:
                payload = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            raise BinanceOrderError(str(payload.get("msg", "Binance rejected the request")), error.code, payload.get("code")) from error
        except (URLError, TimeoutError) as error:
            raise BinanceOrderError("Binance request failed; reconcile by client order ID before retrying", uncertain=True) from error

    def _api_key(self) -> str:
        user_id = current_user_id.get()
        if user_id is not None:
            session = self._session(user_id)
            if session is None or session.api_key is None:
                raise BinanceOrderError("Binance Demo credentials are not configured for this account", 503)
            return session.api_key.get_secret_value()
        value = self._runtime_api_key or self._settings.binance_api_key
        return "" if value is None else value.get_secret_value()

    def _api_secret(self) -> str:
        user_id = current_user_id.get()
        if user_id is not None:
            session = self._session(user_id)
            if session is None or session.api_secret is None:
                raise BinanceOrderError("Binance Demo credentials are not configured for this account", 503)
            return session.api_secret.get_secret_value()
        value = self._runtime_api_secret or self._settings.binance_api_secret
        return "" if value is None else value.get_secret_value()

    @staticmethod
    def _floor(value: str, increment: str | None) -> str:
        number = Decimal(value)
        if not increment or Decimal(increment) == 0:
            return format(number, "f")
        step = Decimal(increment)
        return format((number / step).to_integral_value(rounding=ROUND_DOWN) * step, "f")

    @staticmethod
    def _execution(response: dict[str, Any], fallback: Decimal) -> tuple[Decimal, Decimal]:
        quantity = Decimal(str(response.get("executedQty", "0")))
        quote = Decimal(str(response.get("cummulativeQuoteQty", "0")))
        return quantity, quote / quantity if quantity > 0 and quote > 0 else fallback

    @staticmethod
    def _order_reference(symbol: str, order_id: int | None, client_id: str | None) -> dict[str, Any]:
        if order_id is None and not client_id:
            raise BinanceOrderError("orderId or clientOrderId is required", 422)
        return {"symbol": symbol, **({"orderId": order_id} if order_id is not None else {"origClientOrderId": client_id})}

    @staticmethod
    def _client_id(signal: Signal, suffix: str) -> str:
        return f"cts{signal.variant.value}{signal.symbol}{suffix}{int(signal.timestamp.timestamp())}"[:36]
