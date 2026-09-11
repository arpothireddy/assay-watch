"""Shared fixtures. DB tests use a real Postgres reached via
``ASSAY_TEST_DATABASE_URL`` and are skipped if it is not set or unreachable
-- same pattern the main assay-watch package uses."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from assay_watch.db.models import Base
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

TEST_DB_URL = os.environ.get("ASSAY_TEST_DATABASE_URL")


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
