import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from angel_auto.persistence import db as db_module


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Fresh in-memory SQLite per test - never touches the real data_store/angel_auto.db.
    Autouse + shared here so every test file gets isolated persistence for free."""
    engine = create_engine("sqlite:///:memory:", future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_SessionLocal", session_factory)
    db_module.init_db()
    yield
