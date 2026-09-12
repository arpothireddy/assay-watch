"""Shared fixtures.

Database tests use a real Postgres reached via ``ASSAY_TEST_DATABASE_URL`` and
are skipped if it is not set or unreachable. HTTP is served by
``httpx.MockTransport`` — no network is touched.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assay_watch.db.models import Base
from assay_watch.db.session import get_session

TEST_DB_URL = os.environ.get("ASSAY_TEST_DATABASE_URL")

Handler = Callable[[httpx.Request], httpx.Response]


def make_transport(handler: Handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    if not TEST_DB_URL:
        pytest.skip("ASSAY_TEST_DATABASE_URL not set")
    engine = create_engine(TEST_DB_URL, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"test database unreachable: {exc}")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def clean_db(db_engine: Engine) -> Engine:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE listing_snapshots, crawl_runs RESTART IDENTITY CASCADE"))
    return db_engine


@pytest.fixture
def db_session(clean_db: Engine) -> Iterator[Session]:
    """Built the way the application builds it, not the way a test would.

    This used to construct a bare ``Session``, so the suite never exercised
    the settings production actually runs with -- and the transaction
    behaviour those settings control is precisely what killed a real crawl.
    """
    with get_session(clean_db) as session:
        yield session
