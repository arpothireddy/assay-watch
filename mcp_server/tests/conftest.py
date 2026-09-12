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

# The tool-level tests construct Settings on their way through
# get_settings(), and the reader URL has no default -- so without this the
# whole suite fails on a missing environment variable rather than on
# anything it is testing. Pointed at the test database when there is one, so
# the tools that really do read through to Postgres work off the same
# ASSAY_TEST_DATABASE_URL as everything else here.
os.environ.setdefault(
    "ASSAY_READER_DATABASE_URL", TEST_DB_URL or "postgresql+psycopg://unused/unused"
)


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
