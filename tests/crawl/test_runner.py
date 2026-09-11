from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assay_watch.adapters.base import (
    HealthCheck,
    HealthStatus,
    RawListing,
    Reference,
    SourceAdapter,
)
from assay_watch.crawl import runner as runner_mod
from assay_watch.crawl import storage
from assay_watch.crawl.runner import run_crawl
from assay_watch.db.models import CrawlRun, ListingSnapshot
from assay_watch.settings import Settings
from tests.conftest import TEST_DB_URL, make_transport

_REFS = """
references:
  - ref: "126610LN"
    brand: "Rolex"
    model_name: "Submariner"
    search_aliases: []
    enabled: true
"""
_SOURCES = """
sources:
  - type: shopify
    enabled: true
    min_interval_seconds: 0.0
    stores:
      - name: dealer
        base_url: https://dealer.test
        currency: USD
        enabled: true
  - type: ebay
    enabled: true
"""
_PRODUCTS = [
    {
        "id": 111,
        "title": "Rolex Submariner 126610LN",
        "handle": "sub",
        "variants": [{"sku": "126610LN", "price": "13500.00"}],
    },
    {
        "id": 222,
        "title": "Patek Philippe Nautilus 5711/1A",
        "handle": "nautilus",
        "variants": [{"sku": "5711-1A", "price": "150000.00"}],
    },
]


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/products.json":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json={"products": _PRODUCTS if page == 1 else []})
        return httpx.Response(404)

    return make_transport(handler)


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "refs.yaml").write_text(_REFS)
    (tmp_path / "sources.yaml").write_text(_SOURCES)
    return Settings(
        database_url_override=TEST_DB_URL,
        references_path=tmp_path / "refs.yaml",
        sources_path=tmp_path / "sources.yaml",
        metrics_dir=tmp_path / "metrics",
        user_agent="assay-test/0 (+https://test)",
    )


def test_dry_run_writes_nothing(clean_db: Engine, tmp_path: Path) -> None:
    results = run_crawl(_settings(tmp_path), dry_run=True, transport=_transport())
    by_source = {r.source: r for r in results}
    assert by_source["shopify"].status == "success"
    assert by_source["shopify"].listings_found == 1
    assert by_source["ebay"].status == "skipped"

    with Session(clean_db) as session:
        assert session.scalars(select(ListingSnapshot)).all() == []
        assert session.scalars(select(CrawlRun)).all() == []
    assert (tmp_path / "metrics" / "assay_watch.prom").exists()


def test_real_run_writes_rows(clean_db: Engine, tmp_path: Path) -> None:
    results = run_crawl(_settings(tmp_path), transport=_transport())
    by_source = {r.source: r for r in results}
    assert by_source["shopify"].status == "success"
    assert by_source["ebay"].status == "skipped"

    with Session(clean_db) as session:
        snapshots = session.scalars(select(ListingSnapshot)).all()
        runs = session.scalars(select(CrawlRun)).all()
    assert len(snapshots) == 1
    assert snapshots[0].search_reference == "126610LN"
    assert [r.source for r in runs] == ["shopify"]  # ebay skipped -> no run row
    assert runs[0].status == "success"


class _BrokenAdapter(SourceAdapter):
    name = "broken"

    def health_check(self) -> HealthCheck:
        return HealthCheck(status=HealthStatus.OK)

    def fetch(self, reference: Reference) -> list[RawListing]:
        raise RuntimeError("boom")


def test_broken_adapter_records_failed_run(
    clean_db: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "build_adapters", lambda _s, transport=None: [_BrokenAdapter()])
    results = run_crawl(_settings(tmp_path), transport=_transport())
    assert results[0].status == "failed"

    with Session(clean_db) as session:
        runs = session.scalars(select(CrawlRun)).all()
        snapshots = session.scalars(select(ListingSnapshot)).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error_summary is not None and "boom" in runs[0].error_summary
    assert snapshots == []


def test_db_write_failure_does_not_abort_remaining_references(
    clean_db: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the production incident: a genuine DB error while recording
    one reference's listings must not (a) abort every subsequent reference in
    the same run, or (b) crash the process outright when the run then tries to
    record itself as failed. Postgres aborts the whole transaction on any
    statement error, so the very next commit -- even one just trying to log
    the failure -- raises PendingRollbackError unless something rolls back
    first. That crash is exactly why real crawl_runs rows were stuck at
    status=failed / error_summary=NULL: the process died before finish_run()
    could ever complete."""
    refs = """
references:
  - ref: "126610LN"
    brand: "Rolex"
    model_name: "Submariner"
    search_aliases: []
    enabled: true
  - ref: "5711/1A"
    brand: "Patek"
    model_name: "Nautilus"
    search_aliases: []
    enabled: true
"""
    (tmp_path / "refs.yaml").write_text(refs)
    (tmp_path / "sources.yaml").write_text(_SOURCES)
    settings = Settings(
        database_url_override=TEST_DB_URL,
        references_path=tmp_path / "refs.yaml",
        sources_path=tmp_path / "sources.yaml",
        metrics_dir=tmp_path / "metrics",
        user_agent="assay-test/0 (+https://test)",
    )

    real_record_listings = storage.record_listings

    def poisoned(session: Session, run: object, reference: object, listings: object) -> int:
        if getattr(reference, "ref", None) == "126610LN":
            # A genuine, transaction-aborting SQL error -- the same failure
            # mode as the real incident, not a synthetic Python exception.
            session.execute(text("SELECT * FROM no_such_table"))
        return real_record_listings(session, run, reference, listings)  # type: ignore[arg-type]

    monkeypatch.setattr(storage, "record_listings", poisoned)

    results = run_crawl(settings, transport=_transport())
    result = next(r for r in results if r.source == "shopify")

    # One reference failed, one succeeded -> partial, with a real reason.
    assert result.status == "partial"

    with Session(clean_db) as session:
        runs = session.scalars(select(CrawlRun)).all()
        snapshots = session.scalars(select(ListingSnapshot)).all()
    assert len(runs) == 1
    assert runs[0].status == "partial"
    assert runs[0].error_summary is not None  # not stuck at the NULL pessimistic default
    assert "126610LN" in runs[0].error_summary
    # The un-poisoned reference's listing still made it in.
    assert len(snapshots) == 1
    assert snapshots[0].search_reference == "5711/1A"
