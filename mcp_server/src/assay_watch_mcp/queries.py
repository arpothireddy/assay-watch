"""Read-only pricing queries.

Both queries share the same "current" definition: only listings from the
*most recent* successful-or-partial crawl run per source. Older runs are
history, not current market data -- a price from three weeks ago has no
business being called "the cheapest" or folded into "fair" today.

Only USD listings are priced. Non-USD listings (currently just Bulang &
Sons, GBP) are counted and surfaced separately rather than silently
converted at some FX rate -- that's a real simplification, not an oversight.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine

_LATEST_RUNS = """
    latest_runs AS (
        SELECT DISTINCT ON (source) id, source
        FROM crawl_runs
        WHERE status IN ('success', 'partial')
        ORDER BY source, started_at DESC
    )
"""


class CheapestListing(BaseModel):
    seller_name: str | None
    price_amount: Decimal
    price_currency: str
    raw_title: str
    url: str
    seen_at: str


def get_cheapest_listing(engine: Engine, reference: str) -> CheapestListing | None:
    stmt = text(
        f"""
        WITH {_LATEST_RUNS}
        SELECT ls.seller_name, ls.price_amount, ls.price_currency, ls.raw_title, ls.url, ls.seen_at
        FROM listing_snapshots ls
        JOIN latest_runs lr ON ls.crawl_run_id = lr.id
        WHERE ls.search_reference = :reference
          AND ls.price_currency = 'USD'
          AND ls.price_amount IS NOT NULL
        ORDER BY ls.price_amount ASC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(stmt, {"reference": reference}).mappings().first()
    if row is None:
        return None
    return CheapestListing(
        seller_name=row["seller_name"],
        price_amount=row["price_amount"],
        price_currency=row["price_currency"],
        raw_title=row["raw_title"],
        url=row["url"],
        seen_at=row["seen_at"].isoformat(),
    )


class FairPrice(BaseModel):
    median_price: Decimal
    min_price: Decimal
    max_price: Decimal
    n_listings: int
    excluded_other_currency: int


def get_fair_price(engine: Engine, reference: str) -> FairPrice | None:
    stmt = text(
        f"""
        WITH {_LATEST_RUNS},
        usd AS (
            SELECT ls.price_amount
            FROM listing_snapshots ls
            JOIN latest_runs lr ON ls.crawl_run_id = lr.id
            WHERE ls.search_reference = :reference
              AND ls.price_currency = 'USD'
              AND ls.price_amount IS NOT NULL
        ),
        other AS (
            SELECT COUNT(*) AS n
            FROM listing_snapshots ls
            JOIN latest_runs lr ON ls.crawl_run_id = lr.id
            WHERE ls.search_reference = :reference
              AND ls.price_currency IS DISTINCT FROM 'USD'
        )
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY usd.price_amount) AS median_price,
            MIN(usd.price_amount) AS min_price,
            MAX(usd.price_amount) AS max_price,
            COUNT(usd.price_amount) AS n,
            (SELECT n FROM other) AS excluded
        FROM usd
        """
    )
    with engine.connect() as conn:
        row = conn.execute(stmt, {"reference": reference}).mappings().first()
    if row is None or row["n"] == 0:
        return None
    return FairPrice(
        median_price=row["median_price"],
        min_price=row["min_price"],
        max_price=row["max_price"],
        n_listings=row["n"],
        excluded_other_currency=row["excluded"],
    )
