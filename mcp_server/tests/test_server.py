"""Smoke tests for the actual tool functions the MCP server exposes -- not
just the underlying catalog/query modules already tested in isolation, but
the wiring (settings -> engine/catalog -> tool) itself."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from assay_watch.db.models import CrawlRun, ListingSnapshot
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assay_watch_mcp.server import (
    find_reference_tool,
    get_cheapest_listing_tool,
    get_fair_price_tool,
)


def test_find_reference_tool_resolves_against_the_real_bundled_catalog() -> None:
    """No DB needed -- this exercises the actual config/references.yaml
    shipped in the image, not a test fixture standing in for it."""
    matches = find_reference_tool("Rolex Submariner")
    assert any(m.ref == "126610LN" for m in matches)


def test_pricing_tools_read_through_to_the_configured_database(clean_db: Engine) -> None:
    with Session(clean_db) as session:
        run = CrawlRun(source="shopify", started_at=datetime.now(UTC), status="success")
        session.add(run)
        session.commit()
        session.refresh(run)
        session.add(
            ListingSnapshot(
                source="shopify",
                source_listing_id="x",
                search_reference="126610LN",
                url="https://dealer.test/x",
                raw_title="Watch x",
                price_amount=Decimal("12345.00"),
                price_currency="USD",
                seller_name="dealer",
                raw_payload={},
                content_hash="x",
                crawl_run_id=run.id,
                seen_at=datetime.now(UTC),
            )
        )
        session.commit()

    cheapest = get_cheapest_listing_tool("126610LN")
    assert cheapest is not None
    assert cheapest.price_amount == Decimal("12345.00")

    fair = get_fair_price_tool("126610LN")
    assert fair is not None
    assert fair.n_listings == 1
