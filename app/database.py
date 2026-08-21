"""SQLAlchemy engine / session wiring.

SQLite is used for local development and tests; set DATABASE_URL to a
postgresql+psycopg:// URL in production and nothing else needs to change.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.is_sqlite:
        kwargs: dict = {
            "connect_args": {"check_same_thread": False},
        }
        if ":memory:" in settings.DATABASE_URL:
            kwargs["poolclass"] = StaticPool
        return kwargs
    return {
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
    }


engine = create_engine(settings.DATABASE_URL, future=True, **_engine_kwargs())

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables directly (dev/test). Use Alembic migrations in prod."""
    from app import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
