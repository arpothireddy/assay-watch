"""Crawl orchestration.

Runs every enabled adapter across every enabled reference. Each adapter is
isolated: a source that errors, changes its markup, or goes dark marks *its* run
``failed``, emits a metric, logs, and never stops the other adapters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..adapters.base import HealthStatus, Reference, SourceAdapter
from ..adapters.registry import build_adapters
from ..db.session import create_app_engine, get_session
from ..logging import get_logger
from ..metrics import CrawlMetrics
from ..references import load_enabled_references
from ..settings import Settings, get_settings
from . import storage

log = get_logger(__name__)


@dataclass
class SourceResult:
    source: str
    status: str  # success / partial / failed / skipped
    listings_found: int
    duration_seconds: float


def _iterate(
    adapter: SourceAdapter,
    references: list[Reference],
    session: object | None,
    run: object | None,
) -> tuple[int, int, int, list[str]]:
    """Fetch every reference. Returns (found, n_ok, n_failed, errors)."""
    found = ok = failed = 0
    errors: list[str] = []
    for ref in references:
        try:
            listings = adapter.fetch(ref)
        except Exception as exc:  # one reference failing must not abort the source
            failed += 1
            errors.append(f"{ref.ref}: {type(exc).__name__}: {exc}")
            log.error("crawl.fetch_failed", source=adapter.name, reference=ref.ref, error=str(exc))
            continue
        ok += 1
        found += len(listings)
        if session is not None and run is not None and listings:
            storage.record_listings(session, run, ref, listings)  # type: ignore[arg-type]
    return found, ok, failed, errors


def _status(n_ok: int, n_failed: int) -> str:
    if n_failed == 0:
        return "success"
    if n_ok == 0:
        return "failed"
    return "partial"


def _run_one(
    adapter: SourceAdapter,
    references: list[Reference],
    settings: Settings,
    *,
    dry_run: bool,
) -> SourceResult:
    start = time.monotonic()
    health = adapter.health_check()

    if health.status is HealthStatus.UNCONFIGURED:
        log.info("crawl.skipped", source=adapter.name, detail=health.detail)
        return SourceResult(adapter.name, "skipped", 0, time.monotonic() - start)

    if health.status is HealthStatus.ERROR:
        log.error("crawl.unhealthy", source=adapter.name, detail=health.detail)
        if not dry_run:
            engine = create_app_engine(settings)
            with get_session(engine) as session:
                run = storage.start_run(session, adapter.name)
                storage.finish_run(session, run, "failed", 0, f"health: {health.detail}")
        return SourceResult(adapter.name, "failed", 0, time.monotonic() - start)

    if dry_run:
        found, n_ok, n_failed, _ = _iterate(adapter, references, None, None)
        status = _status(n_ok, n_failed)
        log.info("crawl.dry_run", source=adapter.name, status=status, found=found)
        return SourceResult(adapter.name, status, found, time.monotonic() - start)

    engine = create_app_engine(settings)
    with get_session(engine) as session:
        run = storage.start_run(session, adapter.name)
        try:
            found, n_ok, n_failed, errors = _iterate(adapter, references, session, run)
            status = _status(n_ok, n_failed)
            summary = "; ".join(errors)[:2000] or None
        except Exception as exc:  # catastrophic — the adapter itself blew up
            log.error("crawl.aborted", source=adapter.name, error=str(exc))
            storage.finish_run(session, run, "failed", 0, f"{type(exc).__name__}: {exc}")
            return SourceResult(adapter.name, "failed", 0, time.monotonic() - start)
        storage.finish_run(session, run, status, found, summary)

    log.info("crawl.finished", source=adapter.name, status=status, found=found)
    return SourceResult(adapter.name, status, found, time.monotonic() - start)


def run_crawl(
    settings: Settings | None = None,
    *,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> list[SourceResult]:
    """Run the full crawl. In ``dry_run`` nothing is written to the database."""
    settings = settings or get_settings()
    references = load_enabled_references(settings.references_path)
    adapters = build_adapters(settings, transport=transport)
    metrics = CrawlMetrics()
    results: list[SourceResult] = []

    log.info(
        "crawl.start",
        dry_run=dry_run,
        references=len(references),
        adapters=[a.name for a in adapters],
    )
    for adapter in adapters:
        result = _run_one(adapter, references, settings, dry_run=dry_run)
        metrics.record(result.source, result.status, result.listings_found, result.duration_seconds)
        results.append(result)

    metrics.write(settings.metrics_dir)
    return results
