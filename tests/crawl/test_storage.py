from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from assay_watch.adapters.base import RawListing, Reference
from assay_watch.crawl import storage
from assay_watch.db.models import CrawlRun, ListingSnapshot

REF = Reference(ref="126610LN", brand="Rolex", model_name="Submariner")


def _listing(listing_id: str = "dealer:1") -> RawListing:
    return RawListing(
        source_listing_id=listing_id,
        url="https://dealer.test/p/1",
        raw_title="Rolex Submariner 126610LN",
        price_amount=Decimal("13500"),
        price_currency="USD",
        seller_name="dealer",
        raw_payload={"id": 1, "b": 2},
    )


def test_content_hash_is_order_independent() -> None:
    assert storage.content_hash({"a": 1, "b": 2}) == storage.content_hash({"b": 2, "a": 1})
    assert storage.content_hash({"a": 1}) != storage.content_hash({"a": 2})


def test_start_run_is_pessimistically_failed(db_session: Session) -> None:
    run = storage.start_run(db_session, "shopify")
    assert run.status == "failed"
    assert run.listings_found == 0


def test_snapshots_are_append_only_across_runs(db_session: Session) -> None:
    run1 = storage.start_run(db_session, "shopify")
    storage.record_listings(db_session, run1, REF, [_listing()])
    run2 = storage.start_run(db_session, "shopify")
    storage.record_listings(db_session, run2, REF, [_listing()])

    rows = db_session.scalars(select(ListingSnapshot).order_by(ListingSnapshot.id)).all()
    assert len(rows) == 2  # same listing seen twice = two immutable observations
    assert rows[0].search_reference == "126610LN"
    assert rows[0].content_hash


def test_duplicate_within_run_is_dropped(db_session: Session) -> None:
    run = storage.start_run(db_session, "shopify")
    storage.record_listings(db_session, run, REF, [_listing(), _listing()])
    rows = db_session.scalars(select(ListingSnapshot)).all()
    assert len(rows) == 1


def test_finish_run_updates_status(db_session: Session) -> None:
    run = storage.start_run(db_session, "shopify")
    storage.finish_run(db_session, run, "success", 5)
    refreshed = db_session.get(CrawlRun, run.id)
    assert refreshed is not None
    assert refreshed.status == "success"
    assert refreshed.listings_found == 5
    assert refreshed.finished_at is not None


def test_backfill_report(db_session: Session) -> None:
    run = storage.start_run(db_session, "shopify")
    storage.record_listings(db_session, run, REF, [_listing()])
    rows = storage.backfill_report(db_session)
    assert len(rows) == 1
    assert rows[0].reference == "126610LN"
    assert rows[0].days == 1
    assert rows[0].snapshots == 1


def test_no_transaction_is_left_open_across_the_fetch(db_session: Session) -> None:
    """The crawl holds one session across the whole fetch loop, and a
    transaction left open during that is one Postgres terminates for being
    idle -- which is exactly how a real 21-store run died mid-insert.

    start_run used to refresh() after committing; that SELECT began a fresh
    transaction nothing closed, and the crawl then sat in it for the entire
    fetch phase. Nothing between a commit and the next statement may open
    one, including an innocent-looking attribute read.
    """
    run = storage.start_run(db_session, "shopify")
    assert not db_session.in_transaction(), "start_run left a transaction open"

    # Reading the run's fields is what record_listings does first, before any
    # statement. With expire_on_commit on, these reads alone re-open one.
    _ = (run.id, run.source, run.status)
    assert not db_session.in_transaction(), "reading a committed row re-opened a transaction"

    storage.record_listings(db_session, run, REF, [_listing()])
    assert not db_session.in_transaction(), "record_listings left a transaction open"

    storage.finish_run(db_session, run, "success", 1, None)
    assert not db_session.in_transaction(), "finish_run left a transaction open"
