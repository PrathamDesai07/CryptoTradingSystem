"""Copy every application table from SQLite to Supabase PostgreSQL.

Run from the repository root after setting ``database_password`` in config.yaml:
    python backend/migrate_sqlite_to_supabase.py
The operation is repeatable: rows already present at the destination are skipped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config import get_settings
from services.db_handler import DatabaseHandler


TABLES = (
    "users",
    "user_credentials",
    "sessions",
    "user_strategy_settings",
    "user_positions",
    "order_log",
    "orders",
    "positions",
    "risk_reservations",
    "position_history",
)


def main() -> None:
    settings = get_settings()
    password = settings.database_password.get_secret_value() if settings.database_password else None
    configured_url = settings.database_url.get_secret_value() if settings.database_url else None
    if not password or not configured_url:
        raise SystemExit("Set database_password in config.yaml (or DATABASE_PASSWORD) before migrating.")

    source_path = Path(settings.state_database_path)
    if not source_path.is_absolute():
        source_path = Path(__file__).resolve().parents[1] / source_path
    if not source_path.is_file():
        raise SystemExit(f"SQLite source does not exist: {source_path}")

    target = DatabaseHandler(
        str(source_path),
        settings.strategy_signal_history_size,
        master_key=settings.credentials_master_key.get_secret_value() if settings.credentials_master_key else None,
        database_url=configured_url,
        database_password=password,
    )
    source = sqlite3.connect(source_path)
    destination = target._connect()
    try:
        with destination:
            for table in TABLES:
                columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                if not columns:
                    print(f"{table}: not present in SQLite source, 0 rows copied")
                    continue
                rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
                if rows:
                    names = ",".join(f'"{name}"' for name in columns)
                    placeholders = ",".join("?" for _ in columns)
                    sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
                    for row in rows:
                        destination.execute(sql, tuple(row))
                count = destination.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if count < len(rows):
                    raise RuntimeError(f"Verification failed for {table}: source={len(rows)}, target={count}")
                print(f"{table}: {len(rows)} source rows, {count} destination rows")
            destination.execute(
                "SELECT setval(pg_get_serial_sequence('users','id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM users"
            )
            destination.execute(
                "SELECT setval(pg_get_serial_sequence('order_log','id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM order_log"
            )
    finally:
        source.close()
        destination.close()
    print("Migration completed and table row counts verified.")


if __name__ == "__main__":
    main()
