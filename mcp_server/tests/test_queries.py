from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from assay_watch.db.models import CrawlRun, ListingSnapshot
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assay_watch_mcp.queries import get_cheapest_listing, get_fair_price


def _seed_run(session: Session, *, status: str = "success") -> CrawlRun:
    run = CrawlRun(source="shopify", started_at=datetime.now(UTC), status=status)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _snapshot(
    run: CrawlRun,
    *,
    listing_id: str,
    reference: str,
    price: str,
    currency: str = "USD",
    seller: str = "dealer",
) -> ListingSnapshot:
    return ListingSnapshot(
        source="shopify",
        source_listing_id=listing_id,
        search_reference=reference,
        url=f"https://dealer.test/{listing_id}",
        raw_title=f"Watch {listing_id}",
        price_amount=Decimal(price),
        price_currency=currency,
        seller_name=seller,
        raw_payload={},
        content_hash=listing_id,
        crawl_run_id=run.id,
        seen_at=datetime.now(UTC),
    )


def test_cheapest_listing_picks_the_lowest_price(clean_db: Engine) -> None:
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="14000.00"),
                _snapshot(run, listing_id="b", reference="126610LN", price="12500.00"),
                _snapshot(run, listing_id="c", reference="126610LN", price="13000.00"),
            ]
        )
        session.commit()

    result = get_cheapest_listing(clean_db, "126610LN")
    assert result is not None
    assert result.price_amount == Decimal("12500.00")


def test_cheapest_listing_ignores_older_runs(clean_db: Engine) -> None:
    """Only the most recent run per source counts as "current" -- a
    cheaper price from a stale run must not win."""
    with Session(clean_db) as session:
        old_run = _seed_run(session)
        session.add(_snapshot(old_run, listing_id="old", reference="126610LN", price="9000.00"))
        session.commit()
        new_run = _seed_run(session)
        session.add(_snapshot(new_run, listing_id="new", reference="126610LN", price="13000.00"))
        session.commit()

    result = get_cheapest_listing(clean_db, "126610LN")
    assert result is not None
    assert result.price_amount == Decimal("13000.00")


def test_cheapest_listing_none_when_no_data(clean_db: Engine) -> None:
    assert get_cheapest_listing(clean_db, "does-not-exist") is None


def test_fair_price_is_median_not_average(clean_db: Engine) -> None:
    """A single lowball outlier must not drag the fair price down the way
    an average would."""
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="1.00"),  # outlier
                _snapshot(run, listing_id="b", reference="126610LN", price="13000.00"),
                _snapshot(run, listing_id="c", reference="126610LN", price="13500.00"),
                _snapshot(run, listing_id="d", reference="126610LN", price="14000.00"),
            ]
        )
        session.commit()

    result = get_fair_price(clean_db, "126610LN")
    assert result is not None
    assert result.median_price == Decimal("13250.00")  # between the two middle values
    assert result.n_listings == 4
    assert result.min_price == Decimal("1.00")


def test_fair_price_excludes_and_counts_other_currencies(clean_db: Engine) -> None:
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="13000.00"),
                _snapshot(
                    run, listing_id="b", reference="126610LN", price="9500.00", currency="GBP"
                ),
            ]
        )
        session.commit()

    result = get_fair_price(clean_db, "126610LN")
    assert result is not None
    assert result.n_listings == 1  # the GBP listing isn't priced into this
    assert result.excluded_other_currency == 1


def test_fair_price_none_when_no_usd_data(clean_db: Engine) -> None:
    assert get_fair_price(clean_db, "does-not-exist") is None
