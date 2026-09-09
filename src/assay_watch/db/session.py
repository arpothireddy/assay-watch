"""Database engine and session helpers for the application role."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..settings import Settings, get_settings


def create_app_engine(settings: Settings | None = None) -> Engine:
    """Create an engine bound to the least-privilege application role."""
    settings = settings or get_settings()
    return create_engine(settings.database_url, future=True, pool_pre_ping=True)


@contextmanager
def get_session(engine: Engine) -> Iterator[Session]:
    """Yield a session and always close it. Callers commit explicitly."""
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
