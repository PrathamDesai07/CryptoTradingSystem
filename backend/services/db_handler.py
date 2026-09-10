"""Central SQLite access for users, sessions, credentials and order audit.

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

from cryptography.fernet import Fernet, InvalidToken

from models import Position

_PBKDF2_ITERATIONS = 260_000
_SESSION_TTL = timedelta(days=7)
_KEY_FILE_NAME = ".master_key"


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


class DatabaseHandler:
    """SQLite repository: auth, encrypted credentials and order audit."""

    def __init__(self, path: str, history_limit: int, master_key: str | None = None) -> None:
        target = Path(path)
        if not target.is_absolute():
            target = Path(__file__).resolve().parents[2] / target
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.history_limit = history_limit
        self._fernet = _load_fernet(master_key, self.path.parent)
        self._initialize()
        self._delete_expired_sessions()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS user_credentials (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, api_key_enc TEXT NOT NULL, api_secret_enc TEXT NOT NULL, last_verified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            db.execute("CREATE TABLE IF NOT EXISTS user_strategy_settings (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS user_positions (user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, symbol TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id, symbol, variant))")
            db.execute("""CREATE TABLE IF NOT EXISTS order_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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
                UNIQUE(user_id, record_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_log_user_time ON order_log(user_id, recorded_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_log_user_symbol ON order_log(user_id, symbol, recorded_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_order_log_user_status ON order_log(user_id, status)")
            # Legacy JSON tables used by OrderService restore logic.
            db.execute("CREATE TABLE IF NOT EXISTS orders (record_key TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol_time ON orders(symbol, recorded_at)")
            db.execute("CREATE TABLE IF NOT EXISTS positions (symbol TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol, variant))")

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
                cursor = db.execute(
                    "INSERT INTO users(username, display_name, password_hash, created_at) VALUES(?,?,?,?)",
                    (username, display_name, password_hash, _now()),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("username is already registered") from error
            return int(cursor.lastrowid)

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
                (user_id, int(enabled), _now()),
            )

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
        """Upsert one punched order with queryable status, price, quantity, time."""
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
                       source = excluded.source,
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
            db.execute("INSERT OR REPLACE INTO orders(record_key, recorded_at, symbol, payload) VALUES(?,?,?,?)", (key, record["recordedAt"], record.get("symbol", ""), payload))

    def load_positions(self) -> list[Position]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload FROM positions").fetchall()
        return [Position.model_validate_json(row[0]) for row in rows]

    def save_position(self, position: Position) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO positions(symbol, variant, payload) VALUES(?,?,?)", (position.symbol, position.variant.value, position.model_dump_json()))


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
