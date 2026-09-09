"""Append-only persistence for snapshots and crawl runs.

``listing_snapshots`` rows are only ever inserted. ``crawl_runs`` rows are
inserted at run start (status ``failed``) and updated once to their terminal
status, which is why the application role holds UPDATE on ``crawl_runs`` alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Row, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..adapters.base import RawListing, Reference
from ..db.models import CrawlRun, ListingSnapshot


def content_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonicalised payload — stable across dict ordering."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def start_run(session: Session, source: str) -> CrawlRun:
    """Insert a run row, pessimistically ``failed`` until it completes cleanly."""
    run = CrawlRun(
        source=source,
        started_at=datetime.now(UTC),
        status="failed",
        listings_found=0,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def record_listings(
    session: Session,
    run: CrawlRun,
    reference: Reference,
    listings: Sequence[RawListing],
    *,
    seen_at: datetime | None = None,
) -> int:
    """Insert snapshot rows for one reference. Returns the number of listings
    observed (duplicates within the run are dropped by the unique constraint)."""
    if not listings:
        return 0
    observed_at = seen_at or datetime.now(UTC)
    rows = [
        {
            "source": run.source,
            "source_listing_id": listing.source_listing_id,
            "search_reference": reference.ref,
            "url": listing.url,
            "raw_title": listing.raw_title,
            "price_amount": listing.price_amount,
            "price_currency": listing.price_currency,
            "seller_name": listing.seller_name,
            "seller_country": listing.seller_country,
            "raw_payload": listing.raw_payload,
            "content_hash": content_hash(listing.raw_payload),
            "crawl_run_id": run.id,
            "seen_at": observed_at,
        }
        for listing in listings
    ]
    # Drop duplicate listing ids within this batch (overlapping pagination),
    # keeping the first. The unique constraint additionally guards against
    # duplicates arriving across separate calls in the same run (e.g. a retry).
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        listing_id = str(row["source_listing_id"])
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)
        deduped.append(row)

    stmt = (
        pg_insert(ListingSnapshot)
        .values(deduped)
        .on_conflict_do_nothing(constraint="uq_snapshot_per_run")
    )
    session.execute(stmt)
    session.commit()
    return len(listings)


def finish_run(
    session: Session,
    run: CrawlRun,
    status: str,
    listings_found: int,
    error_summary: str | None = None,
) -> None:
    """Update a run row to its terminal status."""
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.listings_found = listings_found
    run.error_summary = error_summary
    session.commit()


def backfill_report(session: Session) -> Sequence[Row[Any]]:
    """Per-reference history: distinct days observed, total snapshots, last seen."""
    day = func.date(ListingSnapshot.seen_at)
    stmt = (
        select(
            ListingSnapshot.search_reference.label("reference"),
            func.count(func.distinct(day)).label("days"),
            func.count().label("snapshots"),
            func.max(ListingSnapshot.seen_at).label("last_seen"),
        )
        .group_by(ListingSnapshot.search_reference)
        .order_by(ListingSnapshot.search_reference)
    )
    return session.execute(stmt).all()
