import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from angel_auto.persistence import db as db_module


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Fresh in-memory SQLite per test - never touches the real data_store/angel_auto.db.
    Autouse + shared here so every test file gets isolated persistence for free.

    StaticPool + check_same_thread=False: a plain sqlite:///:memory: engine hands out a
    *new*, separate empty in-memory database per connection - fine for single-threaded
    tests, but FastAPI's TestClient dispatches sync routes through a worker thread, which
    would otherwise open a second connection and see "no such table" against a database
    init_db() never touched. StaticPool pins everyone to the one connection that was
    actually initialized.
    """
    engine = create_engine(
        "sqlite:///:memory:", future=True, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_SessionLocal", session_factory)
    db_module.init_db()
    yield
