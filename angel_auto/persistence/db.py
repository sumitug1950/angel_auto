"""SQLAlchemy engine/session setup. SQLite by default (config/config.yaml database.url) -
single-VPS, single-writer workload, zero extra ops. Same ORM models are Postgres-portable
later without changing calling code (see settings.DatabaseConfig).
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from angel_auto.settings import get_settings

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().app.database.url
        if url.startswith("sqlite:///"):
            db_path = Path(url.removeprefix("sqlite:///"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope():
    """with session_scope() as session: ... - commits on success, rolls back on exception."""
    session: Session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables that don't exist yet, then apply any lightweight column/index
    migrations needed on top of an already-existing DB. Safe to call repeatedly (idempotent)."""
    from angel_auto.persistence.migrate import run_lightweight_migrations
    from angel_auto.persistence.models import Base  # local import avoids a circular import

    Base.metadata.create_all(get_engine())
    run_lightweight_migrations()
