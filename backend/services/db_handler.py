"""Central SQLite/PostgreSQL access for users, sessions and order audit.

Everything that touches the database goes through this module so the rest of
the backend talks to one handler wired in ``backend/main.py``.

Tables owned here:
- ``users``            unique usernames with PBKDF2 password hashes
- ``user_credentials`` Fernet-encrypted Binance Demo keys per user
- ``sessions``         revocable bearer tokens (stored hashed)
- ``order_log``        every order punched by a user as queryable columns
- ``orders``/``positions``  legacy JSON tables kept for restart-safe strategy state

Relative database paths resolve against the repository root, so the default
``db/trading_state.db`` lives in the checked-in ``db/`` folder.
"""

import base64
from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

try:
    import psycopg
except ImportError:  # SQLite-only development and unit-test installs remain usable.
    psycopg = None  # type: ignore[assignment]

from cryptography.fernet import Fernet, InvalidToken

from models import Position

_PBKDF2_ITERATIONS = 260_000
_SESSION_TTL = timedelta(days=7)
_KEY_FILE_NAME = ".master_key"
# Stable signed-bigint namespace for the Crypto Trading System execution engine.
_POSTGRES_EXECUTION_LOCK_ID = 4851605261604401987


def hash_password(password: str) -> str:
    """Return a ``pbkdf2_sha256$iterations$salt$digest`` string for storage."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash string."""
    try:
        algorithm, iterations, salt, digest = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt.encode()),
            int(iterations),
        )
        return secrets.compare_digest(
            base64.urlsafe_b64encode(candidate).decode(), digest
        )
    except (ValueError, TypeError):
        return False


class _PostgresConnection:
    """Small DB-API compatibility layer for the repository's portable SQL."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        return self._connection.execute(sql.replace("?", "%s"), parameters)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "_PostgresConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._connection.__exit__(*args)


class DatabaseHandler:
    """Portable repository backed by SQLite or Supabase PostgreSQL."""

    def __init__(self, path: str, history_limit: int, master_key: str | None = None,
                 database_url: str | None = None, database_password: str | None = None) -> None:
        target = Path(path)
        if not target.is_absolute():
            target = Path(__file__).resolve().parents[2] / target
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.database_url = _database_url(database_url, database_password)
        self.is_postgres = bool(self.database_url)
        self.history_limit = history_limit
        self._execution_lease: Any | None = None
        self._fernet = _load_fernet(master_key, self.path.parent)
        self._initialize()
        self._delete_expired_sessions()

    def acquire_execution_lease(self) -> bool:
        """Acquire a process-lifetime lease so only one engine uses this database.

        PostgreSQL advisory locks are session scoped and therefore work across
        Uvicorn workers, hosts, and containers. SQLite deployments use a native
        non-blocking file lock beside the database.
        """
        if self._execution_lease is not None:
            return True
        if self.is_postgres:
            connection = self._connect()
            acquired = bool(
                connection.execute(
                    "SELECT pg_try_advisory_lock(?)",
                    (_POSTGRES_EXECUTION_LOCK_ID,),
                ).fetchone()[0]
            )
            if acquired:
                self._execution_lease = connection
            else:
                connection.close()
            return acquired

        lock_path = self.path.with_suffix(self.path.suffix + ".execution.lock")
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._execution_lease = handle
        return True

    def release_execution_lease(self) -> None:
        """Release the process-lifetime execution lease during clean shutdown."""
        lease = self._execution_lease
        self._execution_lease = None
        if lease is None:
            return
        if not self.is_postgres:
            try:
                if os.name == "nt":
                    import msvcrt

                    lease.seek(0)
                    msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(lease.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            finally:
                lease.close()
            return
        try:
            lease.execute(
                "SELECT pg_advisory_unlock(?)",
                (_POSTGRES_EXECUTION_LOCK_ID,),
            )
        finally:
            lease.close()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> Any:
        if self.is_postgres:
            if psycopg is None:
                raise RuntimeError("PostgreSQL configured but psycopg is not installed")
            if self.database_url is None:
                raise RuntimeError("PostgreSQL connection URL is not configured")
            return _PostgresConnection(psycopg.connect(self.database_url))
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            user_id = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
            order_id = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
            reference_id = "BIGINT" if self.is_postgres else "INTEGER"
            enabled_type = "BOOLEAN NOT NULL DEFAULT TRUE" if self.is_postgres else "INTEGER NOT NULL DEFAULT 1"
            db.execute(f"CREATE TABLE IF NOT EXISTS users (id {user_id}, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute(f"CREATE TABLE IF NOT EXISTS user_credentials (user_id {reference_id} PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, api_key_enc TEXT NOT NULL, api_secret_enc TEXT NOT NULL, last_verified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute(f"CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id {reference_id} NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            db.execute(f"CREATE TABLE IF NOT EXISTS user_strategy_settings (user_id {reference_id} PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, enabled {enabled_type}, updated_at TEXT NOT NULL)")
            db.execute(f"""CREATE TABLE IF NOT EXISTS user_strategy_symbols (
                user_id {reference_id} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                enabled {enabled_type},
                expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, symbol))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_user_strategy_symbols_expiry ON user_strategy_symbols(expires_at)")
            db.execute(f"CREATE TABLE IF NOT EXISTS user_positions (user_id {reference_id} NOT NULL REFERENCES users(id) ON DELETE CASCADE, symbol TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id, symbol, variant))")
            db.execute("""CREATE TABLE IF NOT EXISTS order_log (
                id %s,
                user_id %s NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                record_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT,
                type TEXT,
                status TEXT NOT NULL,
                price TEXT,
                orig_qty TEXT,
                executed_qty TEXT,
                avg_price TEXT,
                strategy_variant TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                order_time TEXT,
                status_time TEXT,
                recorded_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(user_id, record_key))""" % (order_id, reference_id))
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_log_user_time ON order_log(user_id, recorded_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_log_user_symbol ON order_log(user_id, symbol, recorded_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_log_user_status ON order_log(user_id, status)")
            # Legacy JSON tables used by OrderService restore logic.
            db.execute("CREATE TABLE IF NOT EXISTS orders (record_key TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol_time ON orders(symbol, recorded_at)")
            db.execute("CREATE TABLE IF NOT EXISTS positions (symbol TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol, variant))")
            db.execute(f"""CREATE TABLE IF NOT EXISTS risk_reservations (
                reservation_id TEXT PRIMARY KEY,
                account_id {reference_id},
                asset TEXT NOT NULL,
                amount TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_risk_reservations_account ON risk_reservations(account_id, asset)")
            history_pk = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
            db.execute(f"""CREATE TABLE IF NOT EXISTS position_history (
                id {history_pk},
                user_id {reference_id} NOT NULL,
                symbol TEXT NOT NULL,
                variant TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                exit_price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                exit_reason TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(user_id, symbol, variant, closed_at))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_position_history_user ON position_history(user_id, symbol, closed_at DESC)")
            db.execute(f"""CREATE TABLE IF NOT EXISTS order_matches (
                id {history_pk},
                user_id {reference_id} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                entry_order_id BIGINT NOT NULL,
                exit_order_id BIGINT NOT NULL,
                entry_side TEXT NOT NULL,
                exit_side TEXT NOT NULL,
                quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                exit_price TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(user_id, entry_order_id, exit_order_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_matches_user_time ON order_matches(user_id, recorded_at DESC)")

    def _delete_expired_sessions(self) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))

    # ------------------------------------------------------------ credential crypto
    def encrypt_secret(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt_secret(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as error:
            raise ValueError("stored credential could not be decrypted with the current master key") from error

    # ------------------------------------------------------------------ users
    def create_user(self, username: str, display_name: str, password_hash: str) -> int:
        """Create a user and return its id; raise ValueError when the name is taken."""
        with closing(self._connect()) as db, db:
            try:
                suffix = " RETURNING id" if self.is_postgres else ""
                cursor = db.execute(
                    "INSERT INTO users(username, display_name, password_hash, created_at) VALUES(?,?,?,?)" + suffix,
                    (username, display_name, password_hash, _now()),
                )
            except Exception as error:
                if not isinstance(error, sqlite3.IntegrityError) and not (
                    psycopg is not None and isinstance(error, psycopg.IntegrityError)
                ):
                    raise
                raise ValueError("username is already registered") from error
            return int(cursor.fetchone()[0] if self.is_postgres else cursor.lastrowid)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id, username, display_name, password_hash, created_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return _user_row(row)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id, username, display_name, password_hash, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return _user_row(row)

    def delete_user(self, user_id: int) -> None:
        """Remove a user and all dependent account data after failed signup."""
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def set_user_credentials(self, user_id: int, api_key_enc: str, api_secret_enc: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO user_credentials(user_id, api_key_enc, api_secret_enc, created_at, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       api_key_enc = excluded.api_key_enc,
                       api_secret_enc = excluded.api_secret_enc,
                       updated_at = excluded.updated_at,
                       last_verified_at = NULL""",
                (user_id, api_key_enc, api_secret_enc, _now(), _now()),
            )

    def get_user_credentials(self, user_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT api_key_enc, api_secret_enc, last_verified_at, created_at, updated_at FROM user_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "api_key_enc": row[0],
            "api_secret_enc": row[1],
            "last_verified_at": row[2],
            "created_at": row[3],
            "updated_at": row[4],
        }

    def mark_credentials_verified(self, user_id: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE user_credentials SET last_verified_at = ?, updated_at = updated_at WHERE user_id = ?",
                (_now(), user_id),
            )

    # ----------------------------------------------------------------- sessions
    def create_session(self, user_id: int) -> str:
        """Store a hashed bearer token and return the raw token to the client."""
        token = secrets.token_urlsafe(32)
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
                (_hash_token(token), user_id, _now(), _now(offset=_SESSION_TTL)),
            )
        return token

    def get_session_user(self, token: str) -> dict[str, Any] | None:
        token_hash = _hash_token(token)
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT u.id, u.username, u.display_name, u.password_hash, u.created_at
                   FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at >= ?""",
                (token_hash, _now()),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "display_name": row[2],
            "password_hash": row[3],
            "created_at": row[4],
        }

    def delete_session(self, token: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))

    def delete_user_sessions(self, user_id: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def get_user_strategy_enabled(self, user_id: int) -> bool:
        with closing(self._connect()) as db:
            row = db.execute("SELECT enabled FROM user_strategy_settings WHERE user_id = ?", (user_id,)).fetchone()
        return True if row is None else bool(row[0])

    def set_user_strategy_enabled(self, user_id: int, enabled: bool) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO user_strategy_settings(user_id, enabled, updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (user_id, bool(enabled) if self.is_postgres else int(enabled), _now()),
            )

    def get_user_strategy_symbols(self, user_id: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT symbol, enabled, expires_at FROM user_strategy_symbols WHERE user_id = ?", (user_id,)).fetchall()
        return [{"symbol": row[0], "enabled": bool(row[1]), "expires_at": row[2]} for row in rows]

    def set_user_strategy_symbol(self, user_id: int, symbol: str, enabled: bool, expires_at: str | None) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO user_strategy_symbols(user_id, symbol, enabled, expires_at, updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(user_id, symbol) DO UPDATE SET
                   enabled=excluded.enabled, expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                (user_id, symbol, bool(enabled) if self.is_postgres else int(enabled), expires_at, _now()),
            )

    def get_automated_users(self) -> list[tuple[int, str]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT user_id, symbol FROM user_strategy_symbols
                   WHERE enabled = ? AND expires_at > ?""",
                (True if self.is_postgres else 1, _now()),
            ).fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]

    def load_user_positions(self) -> list[tuple[int, Position]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT user_id, payload FROM user_positions").fetchall()
        return [(int(row[0]), Position.model_validate_json(row[1])) for row in rows]

    def save_user_position(self, user_id: int, position: Position) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO user_positions(user_id, symbol, variant, payload, updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id, symbol, variant) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (user_id, position.symbol, position.variant.value, position.model_dump_json(), _now()),
            )

    # ------------------------------------------------------------- order audit
    def log_user_order(self, user_id: int, record: dict[str, Any], source: str = "manual") -> None:
        """Upsert one punched order with queryable status, price, quantity, time.

        ``source`` is only written on first insert. Later status refreshes from
        query/cancel paths omit it, and an order's origin never changes, so the
        conflict update must not overwrite it (that downgraded strategy orders
        to ``manual``).
        """
        record_key = str(record.get("clientOrderId") or record.get("newClientOrderId") or record.get("orderId") or record.get("recordedAt"))
        recorded_at = str(record.get("recordedAt") or _now())
        executed_qty = str(record.get("executedQty") or record.get("origQty") or record.get("quantity") or "0")
        avg_price = _average_price(record)
        payload = json.dumps(record, default=str, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO order_log(
                       user_id, record_key, symbol, side, type, status, price,
                       orig_qty, executed_qty, avg_price, strategy_variant, source,
                       order_time, status_time, recorded_at, payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, record_key) DO UPDATE SET
                       symbol = excluded.symbol,
                       side = excluded.side,
                       type = excluded.type,
                       status = excluded.status,
                       price = excluded.price,
                       orig_qty = excluded.orig_qty,
                       executed_qty = excluded.executed_qty,
                       avg_price = excluded.avg_price,
                       strategy_variant = excluded.strategy_variant,
                       order_time = excluded.order_time,
                       status_time = excluded.status_time,
                       recorded_at = excluded.recorded_at,
                       payload = excluded.payload""",
                (
                    user_id,
                    record_key,
                    str(record.get("symbol") or ""),
                    str(record.get("side") or "") or None,
                    str(record.get("type") or "") or None,
                    str(record.get("status") or "UNKNOWN"),
                    str(record["price"]) if record.get("price") is not None else None,
                    str(record.get("origQty") or record.get("quantity") or "") or None,
                    executed_qty,
                    avg_price or None,
                    str(record.get("strategy_variant") or "") or None,
                    source,
                    _binance_time(record.get("time") or record.get("transactTime")),
                    _binance_time(record.get("updateTime") or record.get("transactTime")) or recorded_at,
                    recorded_at,
                    payload,
                ),
            )

    def get_user_orders(self, user_id: int, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first order log rows for one user, optionally per symbol."""
        with closing(self._connect()) as db:
            if symbol:
                rows = db.execute(
                    "SELECT id, user_id, record_key, symbol, side, type, status, price, orig_qty, executed_qty, avg_price, strategy_variant, source, order_time, status_time, recorded_at, payload FROM order_log WHERE user_id = ? AND symbol = ? ORDER BY recorded_at DESC, id DESC LIMIT ?",
                    (user_id, symbol, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, user_id, record_key, symbol, side, type, status, price, orig_qty, executed_qty, avg_price, strategy_variant, source, order_time, status_time, recorded_at, payload FROM order_log WHERE user_id = ? ORDER BY recorded_at DESC, id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        return [_order_log_row(row) for row in rows]

    # ------------------------------------------ legacy OrderService repository API
    def load_orders(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload FROM orders ORDER BY recorded_at DESC LIMIT ?", (self.history_limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def save_order(self, record: dict[str, Any]) -> None:
        key = str(record.get("clientOrderId") or record.get("newClientOrderId") or record.get("orderId") or record["recordedAt"])
        payload = json.dumps(record, default=str, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO orders(record_key, recorded_at, symbol, payload) VALUES(?,?,?,?) ON CONFLICT(record_key) DO UPDATE SET recorded_at=excluded.recorded_at, symbol=excluded.symbol, payload=excluded.payload", (key, record["recordedAt"], record.get("symbol", ""), payload))

    def load_positions(self) -> list[Position]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload FROM positions").fetchall()
        return [Position.model_validate_json(row[0]) for row in rows]

    def save_position(self, position: Position) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO positions(symbol, variant, payload) VALUES(?,?,?) ON CONFLICT(symbol, variant) DO UPDATE SET payload=excluded.payload", (position.symbol, position.variant.value, position.model_dump_json()))

    def record_position_history(self, user_id: int, position: Position, exit_reason: str) -> None:
        """Append one closed position to the append-only history, once."""
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO position_history(
                       user_id, symbol, variant, entry_price, exit_price, quantity,
                       realized_pnl, exit_reason, opened_at, closed_at, recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (
                    user_id,
                    position.symbol,
                    position.variant.value,
                    str(position.entry_price),
                    str(position.current_price),
                    str(position.quantity),
                    str(position.current_pnl),
                    exit_reason,
                    position.opened_at.isoformat(),
                    (position.closed_at or position.opened_at).isoformat(),
                    _now(),
                ),
            )

    def get_position_history(self, user_id: int, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first closed positions for one account, optionally per symbol."""
        columns = ("symbol", "variant", "entry_price", "exit_price", "quantity", "realized_pnl", "exit_reason", "opened_at", "closed_at")
        statement = "SELECT symbol, variant, entry_price, exit_price, quantity, realized_pnl, exit_reason, opened_at, closed_at FROM position_history WHERE user_id = ?"
        parameters: tuple[Any, ...] = (user_id,)
        if symbol:
            statement += " AND symbol = ?"
            parameters += (symbol,)
        statement += " ORDER BY closed_at DESC LIMIT ?"
        parameters += (limit,)
        with closing(self._connect()) as db:
            rows = db.execute(statement, parameters).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def record_order_match(self, user_id: int, match: dict[str, Any]) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO order_matches(
                       user_id, symbol, entry_order_id, exit_order_id,
                       entry_side, exit_side, quantity, entry_price,
                       exit_price, realized_pnl, recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id, entry_order_id, exit_order_id) DO NOTHING""",
                (
                    user_id, match["symbol"], int(match["entry_order_id"]), int(match["exit_order_id"]),
                    match["entry_side"], match["exit_side"], str(match["quantity"]),
                    str(match["entry_price"]), str(match["exit_price"]), str(match["realized_pnl"]),
                    match["recorded_at"],
                ),
            )

    def get_order_matches(self, user_id: int, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        statement = "SELECT symbol, entry_order_id, exit_order_id, entry_side, exit_side, quantity, entry_price, exit_price, realized_pnl, recorded_at FROM order_matches WHERE user_id = ?"
        parameters: tuple[Any, ...] = (user_id,)
        if symbol:
            statement += " AND symbol = ?"
            parameters += (symbol,)
        statement += " ORDER BY recorded_at DESC LIMIT ?"
        parameters += (limit,)
        with closing(self._connect()) as db:
            rows = db.execute(statement, parameters).fetchall()
        columns = ("symbol", "entry_order_id", "exit_order_id", "entry_side", "exit_side", "quantity", "entry_price", "exit_price", "realized_pnl", "recorded_at")
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def load_risk_reservations(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT reservation_id, account_id, asset, amount, client_order_id, symbol, side, status FROM risk_reservations"
            ).fetchall()
        columns = ("reservation_id", "account_id", "asset", "amount", "client_order_id", "symbol", "side", "status")
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def save_risk_reservation(self, reservation: Any, status: str) -> None:
        now = _now()
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO risk_reservations(reservation_id, account_id, asset, amount, client_order_id, symbol, side, status, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(reservation_id) DO UPDATE SET
                   amount=excluded.amount, status=excluded.status, updated_at=excluded.updated_at""",
                (reservation.reservation_id, reservation.account_id, reservation.asset, str(reservation.amount),
                 reservation.client_order_id, reservation.symbol, reservation.side, status, now, now),
            )

    def delete_risk_reservation(self, reservation_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM risk_reservations WHERE reservation_id = ?", (reservation_id,))


def _database_url(database_url: str | None, password: str | None) -> str | None:
    """Insert a separately configured password into a PostgreSQL URL safely."""
    if not database_url:
        return None
    if "[YOUR-PASSWORD]" in database_url and not password:
        return None
    if "[YOUR-PASSWORD]" in database_url:
        database_url = database_url.replace(":[YOUR-PASSWORD]@", "@")
    parts = urlsplit(database_url.strip())
    if parts.scheme not in {"postgresql", "postgres"}:
        raise ValueError("DATABASE_URL must use postgresql://")
    if password and parts.hostname:
        username = quote(parts.username or "postgres", safe="")
        host = parts.hostname
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{username}:{quote(password, safe='')}@{host}{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return database_url.strip()


def _now(offset: timedelta | None = None) -> str:
    timestamp = datetime.now(UTC) + (offset or timedelta())
    return timestamp.isoformat().replace("+00:00", "Z")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_fernet(master_key: str | None, key_directory: Path) -> Fernet:
    if master_key:
        try:
            return Fernet(master_key.strip().encode())
        except (TypeError, ValueError) as error:
            raise ValueError("CREDENTIALS_MASTER_KEY must be a urlsafe-base64 Fernet key (44 characters)") from error
    key_file = key_directory / _KEY_FILE_NAME
    if key_file.is_file():
        return Fernet(key_file.read_text(encoding="utf-8").strip().encode())
    key = Fernet.generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(key_file, flags, 0o600)
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    return Fernet(key)


def _user_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2],
        "password_hash": row[3],
        "created_at": row[4],
    }


def _average_price(record: dict[str, Any]) -> str | None:
    executed = record.get("executedQty")
    quote = record.get("cummulativeQuoteQty")
    if executed in (None, "") or quote in (None, ""):
        return str(record["price"]) if record.get("price") is not None else str(record.get("avgPrice") or "") or None
    try:
        quantity = float(executed)
        if quantity <= 0:
            return None
        return format(float(quote) / quantity, ".8f").rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return None


def _binance_time(value: Any) -> str | None:
    """Convert a Binance millisecond epoch (int or numeric string) to UTC ISO."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _order_log_row(row: Any) -> dict[str, Any]:
    columns = ("id", "user_id", "record_key", "symbol", "side", "type", "status", "price", "orig_qty", "executed_qty", "avg_price", "strategy_variant", "source", "order_time", "status_time", "recorded_at", "payload")
    item = dict(zip(columns, row, strict=True))
    item["payload"] = json.loads(item["payload"])
    return item
