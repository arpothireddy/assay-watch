"""SQLAlchemy models — the two Phase 0 tables.

Design invariants:

- ``listing_snapshots`` is append-only. The application role is granted
  INSERT/SELECT only on it (no UPDATE, no DELETE), so append-only is enforced by
  the database, not merely by convention.
- ``crawl_runs`` is operational metadata, not the raw asset. A run row is
  inserted at the start (pessimistically ``failed``) and updated to its terminal
  status when the run completes, so a crashed process truthfully leaves a
  ``failed`` row behind. The application role therefore also holds UPDATE on
  ``crawl_runs`` only.
- Nothing here parses references, condition, or completeness. That is Phase 2,
  built as a separate stage against preserved raw. The only forward-compatible
  hook is ``content_hash`` (see below).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CrawlRun(Base):
    """One row per adapter execution."""

    __tablename__ = "crawl_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # success / partial / failed. Initialised to "failed" and upgraded on clean
    # completion, so an interrupted run is never mistaken for a success.
    status: Mapped[str] = mapped_column(Text, default="failed")
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ListingSnapshot(Base):
    """An immutable observation of one listing during one crawl run."""

    __tablename__ = "listing_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text)
    source_listing_id: Mapped[str] = mapped_column(Text)
    # The registry reference the adapter was querying when it found this listing.
    # Captured at fetch time because it is unrecoverable later and is required for
    # per-reference history and comp-density reporting.
    search_reference: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    raw_title: Mapped[str] = mapped_column(Text)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    price_currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_country: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The source's own response payload, stored verbatim. The promoted columns
    # above are conveniences; this is the ground truth everything downstream is
    # re-derived from.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # SHA-256 of the canonicalised raw_payload. The single hook the later cached,
    # re-runnable extraction stage keys on (extract once per content hash, not
    # once per snapshot) and a cheap change-detection signal in the raw layer.
    content_hash: Mapped[str] = mapped_column(Text)
    crawl_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crawl_runs.id"))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Guards against a double-write of the same listing within one run (e.g.
        # overlapping pagination or a retry). It deliberately does NOT dedupe
        # across runs — repeated observations across runs are the time-series.
        UniqueConstraint("source", "source_listing_id", "crawl_run_id", name="uq_snapshot_per_run"),
    )


# The per-listing history access pattern: latest-first snapshots for a listing.
# A plain btree is reverse-scannable, but declaring the sort makes intent explicit.
Index(
    "ix_snapshot_source_listing_seen",
    ListingSnapshot.source,
    ListingSnapshot.source_listing_id,
    ListingSnapshot.seen_at.desc(),
)
# Content-hash lookups for change detection and (Phase 2) extraction caching.
Index("ix_snapshot_content_hash", ListingSnapshot.content_hash)
