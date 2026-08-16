from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from angel_auto.persistence import db as db_module
from angel_auto.persistence.migrate import run_lightweight_migrations


def _build_old_schema_engine():
    """A DB matching the schema BEFORE strategy_name/charges_rs/net_pnl_rs existed - trade_date
    was globally UNIQUE on daily_risk_state (one row/day for the whole app)."""
    engine = create_engine(
        "sqlite:///:memory:", future=True, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE positions (
                    id INTEGER PRIMARY KEY,
                    direction_request_id INTEGER,
                    direction VARCHAR(10),
                    structure_type VARCHAR(20),
                    status VARCHAR(20),
                    expiry VARCHAR(20),
                    entry_time DATETIME,
                    exit_time DATETIME,
                    exit_reason VARCHAR(20),
                    entry_net_premium_rs FLOAT,
                    realized_pnl_rs FLOAT,
                    peak_profit_rs FLOAT,
                    trail_active BOOLEAN
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE daily_risk_state (
                    id INTEGER PRIMARY KEY,
                    trade_date DATE,
                    trades_taken INTEGER,
                    realized_pnl_rs FLOAT,
                    consecutive_losses INTEGER,
                    trading_halted BOOLEAN,
                    halt_reason VARCHAR(200),
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(text("CREATE UNIQUE INDEX ix_daily_risk_state_trade_date ON daily_risk_state (trade_date)"))
        conn.execute(
            text(
                "INSERT INTO positions (id, direction, structure_type, status, expiry) "
                "VALUES (1, 'LONG', 'DEBIT', 'CLOSED', '18AUG2026')"
            )
        )
        conn.execute(text("INSERT INTO daily_risk_state (id, trade_date, trades_taken) VALUES (1, '2026-08-16', 1)"))
    return engine


def test_migration_adds_missing_columns_and_backfills_existing_rows(monkeypatch):
    engine = _build_old_schema_engine()
    monkeypatch.setattr(db_module, "_engine", engine)

    run_lightweight_migrations()

    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(positions)").fetchall()}
        assert {"strategy_name", "charges_rs", "net_pnl_rs"} <= cols
        row = conn.execute(text("SELECT strategy_name FROM positions WHERE id = 1")).fetchone()
        assert row[0] == "macd_itm_otm_spread"

        daily_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(daily_risk_state)").fetchall()}
        assert "strategy_name" in daily_cols
        row = conn.execute(text("SELECT strategy_name FROM daily_risk_state WHERE id = 1")).fetchone()
        assert row[0] == "macd_itm_otm_spread"


def test_migration_drops_unique_index_allowing_multiple_strategies_same_day(monkeypatch):
    engine = _build_old_schema_engine()
    monkeypatch.setattr(db_module, "_engine", engine)
    run_lightweight_migrations()

    with engine.begin() as conn:
        # would raise IntegrityError under the old single-column UNIQUE(trade_date)
        conn.execute(
            text(
                "INSERT INTO daily_risk_state (id, trade_date, strategy_name, trades_taken) "
                "VALUES (2, '2026-08-16', 'atm_sell_macd_zero', 0)"
            )
        )
        count = conn.execute(
            text("SELECT COUNT(*) FROM daily_risk_state WHERE trade_date = '2026-08-16'")
        ).fetchone()[0]
        assert count == 2


def test_migration_is_idempotent(monkeypatch):
    engine = _build_old_schema_engine()
    monkeypatch.setattr(db_module, "_engine", engine)
    run_lightweight_migrations()
    run_lightweight_migrations()  # must not raise on a second call
