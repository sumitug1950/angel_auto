"""Lightweight schema migrations for the existing SQLite DB - this codebase has no Alembic
(init_db() just calls Base.metadata.create_all(), which only creates missing tables, never
alters existing ones). Adding columns to already-existing tables needs a manual step, done
here with raw PRAGMA/ALTER TABLE and re-checked (idempotent) on every startup.

A fresh database created by create_all() from the current models already has every column
this touches, so every check below is a no-op there - this only matters for upgrading a
pre-existing angel_auto.db.
"""
from __future__ import annotations

from sqlalchemy import Connection

from angel_auto.logging_conf import get_logger
from angel_auto.persistence.db import get_engine

log = get_logger(__name__)

DEFAULT_STRATEGY_NAME = "macd_itm_otm_spread"


def run_lightweight_migrations() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        _ensure_column(conn, "positions", "strategy_name", f"VARCHAR(50) NOT NULL DEFAULT '{DEFAULT_STRATEGY_NAME}'")
        _ensure_column(conn, "positions", "charges_rs", "FLOAT DEFAULT 0.0")
        _ensure_column(conn, "positions", "net_pnl_rs", "FLOAT")
        _ensure_column(conn, "daily_risk_state", "strategy_name", f"VARCHAR(50) NOT NULL DEFAULT '{DEFAULT_STRATEGY_NAME}'")
        # trade_date used to be globally UNIQUE (one row/day for the whole app); now it's one
        # row per (trade_date, strategy_name), so any old unique index on trade_date alone
        # must go, or a second strategy's first trade of the day would fail to insert.
        _drop_unique_index_on(conn, "daily_risk_state", "trade_date")


def _table_columns(conn: Connection, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _ensure_column(conn: Connection, table: str, column: str, ddl_type: str) -> None:
    if column in _table_columns(conn, table):
        return
    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    log.info("migration_column_added", table=table, column=column)


def _drop_unique_index_on(conn: Connection, table: str, column: str) -> None:
    indexes = conn.exec_driver_sql(f"PRAGMA index_list({table})").fetchall()
    for row in indexes:
        index_name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        index_cols = conn.exec_driver_sql(f"PRAGMA index_info({index_name})").fetchall()
        col_names = {c[2] for c in index_cols}
        if col_names == {column}:
            conn.exec_driver_sql(f"DROP INDEX {index_name}")
            log.info("migration_unique_index_dropped", table=table, index=index_name)
