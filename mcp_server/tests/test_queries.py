from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from assay_watch.db.models import CrawlRun, ListingSnapshot
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assay_watch_mcp.queries import get_cheapest_listing, get_fair_price, list_listings


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
    """One dealer asking far above the rest must not drag the fair price up
    the way an average would. The high side is where the median has to do
    this work on its own: only the low side is filtered."""
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="13000.00"),
                _snapshot(run, listing_id="b", reference="126610LN", price="13500.00"),
                _snapshot(run, listing_id="c", reference="126610LN", price="14000.00"),
                _snapshot(run, listing_id="d", reference="126610LN", price="60000.00"),
            ]
        )
        session.commit()

    result = get_fair_price(clean_db, "126610LN")
    assert result is not None
    assert result.median_price == Decimal("13750.00")  # between the two middle values
    assert result.n_listings == 4  # the high outlier is kept, just not influential
    assert result.max_price == Decimal("60000.00")


def test_a_one_dollar_listing_is_not_priced_as_the_watch(clean_db: Engine) -> None:
    """The previous version of this module kept a $1.00 row and leaned on the
    median to absorb it. That is fine for the median and wrong for "cheapest
    listing", which is the number a buyer actually acts on."""
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="1.00"),
                _snapshot(run, listing_id="b", reference="126610LN", price="13000.00"),
                _snapshot(run, listing_id="c", reference="126610LN", price="13500.00"),
                _snapshot(run, listing_id="d", reference="126610LN", price="14000.00"),
            ]
        )
        session.commit()

    cheapest = get_cheapest_listing(clean_db, "126610LN")
    assert cheapest is not None
    assert cheapest.price_amount == Decimal("13000.00")

    result = get_fair_price(clean_db, "126610LN")
    assert result is not None
    assert result.n_listings == 3
    assert result.excluded_implausible == 1
    assert result.min_price == Decimal("13000.00")


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


def _titled(
    run: CrawlRun, *, listing_id: str, reference: str, price: str, title: str
) -> ListingSnapshot:
    snap = _snapshot(run, listing_id=listing_id, reference=reference, price=price)
    snap.raw_title = title
    return snap


def test_implausibly_cheap_listing_is_not_the_cheapest(clean_db: Engine) -> None:
    """The shape that reached production: a $29 strap filed under a Patek
    Nautilus made the site report $29 as the cheapest one on the market."""
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _titled(
                    run,
                    listing_id="strap",
                    reference="5712/1A",
                    price="29.00",
                    title="Old Fashioned Brown II Alligator-Pattern Strap",
                ),
                _titled(
                    run,
                    listing_id="real",
                    reference="5712/1A",
                    price="85000.00",
                    title="Patek Philippe Nautilus 5712A",
                ),
                _titled(
                    run,
                    listing_id="real2",
                    reference="5712/1A",
                    price="91000.00",
                    title="Patek Philippe Nautilus 5712/1A",
                ),
            ]
        )
        session.commit()

    cheapest = get_cheapest_listing(clean_db, "5712/1A")
    assert cheapest is not None
    assert cheapest.price_amount == Decimal("85000.00")

    fair = get_fair_price(clean_db, "5712/1A")
    assert fair is not None
    assert fair.n_listings == 2
    assert fair.excluded_implausible == 1
    assert fair.min_price == Decimal("85000.00")


def test_a_deposit_listing_is_excluded_by_title(clean_db: Engine) -> None:
    """An authorised dealer's deposit against an order legitimately carries
    the reference in its title, so matching cannot reject it."""
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _titled(
                    run,
                    listing_id="dep",
                    reference="210.30.42.20.01.001",
                    price="1000.00",
                    title="Deposit - Omega Seamaster Diver 300M 210.30.42.20.01.001",
                ),
                _titled(
                    run,
                    listing_id="w1",
                    reference="210.30.42.20.01.001",
                    price="4800.00",
                    title="Omega Seamaster Diver 300M",
                ),
                _titled(
                    run,
                    listing_id="w2",
                    reference="210.30.42.20.01.001",
                    price="5200.00",
                    title="Omega Seamaster Diver 300M Steel",
                ),
            ]
        )
        session.commit()

    cheapest = get_cheapest_listing(clean_db, "210.30.42.20.01.001")
    assert cheapest is not None
    assert cheapest.price_amount == Decimal("4800.00")


def test_a_keenly_priced_listing_is_kept(clean_db: Engine) -> None:
    """The guard must not quietly delete real bargains -- only prices that
    are not credible as the watch at all."""
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="13000.00"),
                _snapshot(run, listing_id="b", reference="126610LN", price="13500.00"),
                # ~28% under the median: aggressive, but a real listing.
                _snapshot(run, listing_id="c", reference="126610LN", price="9400.00"),
            ]
        )
        session.commit()

    cheapest = get_cheapest_listing(clean_db, "126610LN")
    assert cheapest is not None
    assert cheapest.price_amount == Decimal("9400.00")

    fair = get_fair_price(clean_db, "126610LN")
    assert fair is not None
    assert fair.n_listings == 3
    assert fair.excluded_implausible == 0


def test_list_listings_returns_the_working_behind_the_numbers(clean_db: Engine) -> None:
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add_all(
            [
                _snapshot(run, listing_id="a", reference="126610LN", price="14000.00"),
                _snapshot(run, listing_id="b", reference="126610LN", price="12500.00"),
                _snapshot(run, listing_id="c", reference="126610LN", price="13000.00"),
                _snapshot(
                    run, listing_id="gbp", reference="126610LN", price="11000.00", currency="GBP"
                ),
            ]
        )
        session.commit()

    listings = list_listings(clean_db, "126610LN")
    assert [str(item.price_amount) for item in listings] == ["12500.00", "13000.00", "14000.00"]
    assert all(item.price_currency == "USD" for item in listings)


def test_list_listings_is_empty_for_an_untracked_reference(clean_db: Engine) -> None:
    with Session(clean_db) as session:
        run = _seed_run(session)
        session.add(_snapshot(run, listing_id="a", reference="126610LN", price="14000.00"))
        session.commit()

    assert list_listings(clean_db, "999999") == []
