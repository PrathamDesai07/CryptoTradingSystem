"""Small SQLite repository for restart-safe orders and strategy positions."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from models import Position


class StateRepository:
    def __init__(self, path: str, history_limit: int) -> None:
        target = Path(path)
        if not target.is_absolute():
            target = Path(__file__).resolve().parents[2] / target
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.history_limit = history_limit
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (record_key TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol_time ON orders(symbol, recorded_at)")
            db.execute("CREATE TABLE IF NOT EXISTS positions (symbol TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol, variant))")

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
