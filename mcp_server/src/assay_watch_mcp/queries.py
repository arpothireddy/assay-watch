"""Read-only pricing queries.

Every query shares one definition of what counts. Three filters, in order:

*Current* -- only listings from the most recent successful-or-partial crawl
run per source. Older runs are history, not current market data: a price
from three weeks ago has no business being called "the cheapest" today.

*Priced in USD* -- non-USD listings (currently just Bulang & Sons, GBP) are
counted and surfaced separately rather than silently converted at some FX
rate. A real simplification, not an oversight.

*Plausible* -- a listing whose price is wildly out of line with the rest of
that reference's listings is not priced as an example of it. Dealers list
things that carry a reference number without being the watch: deposits
against an order, service charges, spare parts. Matching cannot tell these
apart, because their titles legitimately contain the reference; price can.
The median is taken first and listings far below it are dropped, which also
catches the odd coincidental SKU match. The guard leans on most listings
for a reference being real -- it would mislead if most were junk, so it is
a safety net under correct matching, not a substitute for it.

Filtering happens here rather than at crawl time on purpose: the snapshot
rows stay a faithful record of what each dealer actually published, and
these thresholds can be retuned without re-crawling.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import RowMapping, text
from sqlalchemy.engine import Engine

# A fraction of the reference's own median. Wide on purpose: the job is to
# catch a $29 strap filed under a $100k Patek, not to second-guess a dealer
# pricing keenly. A genuine listing 65% below the median of its peers would
# be an extraordinary find; a $29 one is a data error.
#
# Only a floor, deliberately. The number this protects is "the cheapest
# listing", which only cheap junk corrupts, and the median already resists a
# high outlier on its own. A ceiling would buy nothing and would risk hiding
# real listings that are legitimately multiples of the median -- an unworn
# full-set example, or a precious-metal variant sharing the reference.
_PLAUSIBLE_FLOOR = 0.35

# Titles that name something other than the watch itself. Kept deliberately
# short and unambiguous: "strap" and "bracelet" are NOT here, because
# "Speedmaster on leather strap" and "Submariner on bracelet" are listings
# for the watch. The price guard above is what catches those accessories.
_NON_WATCH_TITLE = r"(deposit|gift ?card|spring ?bar|watch ?winder|service (fee|charge)|poster)"

_CURRENT = """
    latest_runs AS (
        SELECT DISTINCT ON (source) id, source
        FROM crawl_runs
        WHERE status IN ('success', 'partial')
        ORDER BY source, started_at DESC
    ),
    current AS (
        SELECT ls.*
        FROM listing_snapshots ls
        JOIN latest_runs lr ON ls.crawl_run_id = lr.id
        WHERE ls.search_reference = :reference
    ),
    priced AS (
        SELECT * FROM current
        WHERE price_currency = 'USD'
          AND price_amount IS NOT NULL
          AND raw_title !~* :non_watch
    ),
    ref_median AS (
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price_amount) AS m FROM priced
    ),
    plausible AS (
        SELECT p.* FROM priced p, ref_median r
        WHERE r.m IS NULL OR p.price_amount >= r.m * :floor
    )
"""


def _params(reference: str) -> dict[str, object]:
    return {
        "reference": reference,
        "non_watch": _NON_WATCH_TITLE,
        "floor": _PLAUSIBLE_FLOOR,
    }


class Listing(BaseModel):
    seller_name: str | None
    price_amount: Decimal
    price_currency: str
    raw_title: str
    url: str
    seen_at: str


def _to_listing(row: RowMapping) -> Listing:
    return Listing(
        seller_name=row["seller_name"],
        price_amount=row["price_amount"],
        price_currency=row["price_currency"],
        raw_title=row["raw_title"],
        url=row["url"],
        seen_at=row["seen_at"].isoformat(),
    )


def get_cheapest_listing(engine: Engine, reference: str) -> Listing | None:
    stmt = text(
        f"""
        WITH {_CURRENT}
        SELECT seller_name, price_amount, price_currency, raw_title, url, seen_at
        FROM plausible
        ORDER BY price_amount ASC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(stmt, _params(reference)).mappings().first()
    return _to_listing(row) if row is not None else None


def list_listings(engine: Engine, reference: str, limit: int = 100) -> list[Listing]:
    """Every current, plausible USD listing for a reference, cheapest first.

    What the cheapest-listing and fair-price numbers are actually computed
    from -- so a caller can show the working rather than asking for the
    summary to be taken on faith.
    """
    stmt = text(
        f"""
        WITH {_CURRENT}
        SELECT seller_name, price_amount, price_currency, raw_title, url, seen_at
        FROM plausible
        ORDER BY price_amount ASC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt, {**_params(reference), "limit": limit}).mappings().all()
    return [_to_listing(r) for r in rows]


class FairPrice(BaseModel):
    median_price: Decimal
    min_price: Decimal
    max_price: Decimal
    n_listings: int
    excluded_other_currency: int
    excluded_implausible: int


def get_fair_price(engine: Engine, reference: str) -> FairPrice | None:
    stmt = text(
        f"""
        WITH {_CURRENT},
        other AS (
            SELECT COUNT(*) AS n FROM current
            WHERE price_currency IS DISTINCT FROM 'USD'
        ),
        dropped AS (
            SELECT (SELECT COUNT(*) FROM priced) - (SELECT COUNT(*) FROM plausible) AS n
        )
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY plausible.price_amount) AS median_price,
            MIN(plausible.price_amount) AS min_price,
            MAX(plausible.price_amount) AS max_price,
            COUNT(plausible.price_amount) AS n,
            (SELECT n FROM other) AS excluded,
            (SELECT n FROM dropped) AS implausible
        FROM plausible
        """
    )
    with engine.connect() as conn:
        row = conn.execute(stmt, _params(reference)).mappings().first()
    if row is None or row["n"] == 0:
        return None
    return FairPrice(
        median_price=row["median_price"],
        min_price=row["min_price"],
        max_price=row["max_price"],
        n_listings=row["n"],
        excluded_other_currency=row["excluded"],
        excluded_implausible=row["implausible"],
    )
