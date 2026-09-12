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
    """Yield a session and always close it. Callers commit explicitly.

    ``expire_on_commit`` is off because the crawl holds one session across
    long stretches of network I/O. With it on, simply reading an attribute of
    a committed object emits a SELECT to reload it, and that SELECT opens a
    transaction which then sits idle for as long as the next fetch takes --
    until Postgres terminates the connection for being idle in transaction.
    Nothing here re-reads a row expecting to see another writer's changes, so
    keeping the loaded values is both safe and what the caller wants.
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
