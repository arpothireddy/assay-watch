"""The MCP server: wires the catalog matcher and pricing queries as tools.

Runs over streamable-http so it's reachable over the network (Cloud Run,
or any remote MCP client) rather than only over stdio.
"""

from __future__ import annotations

from functools import lru_cache

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from . import queries
from .catalog import CatalogEntry, Reference, ReferenceMatch, Specs, find_reference, load_catalog
from .fetcher import ListingSource, TTLCache, WatchFetcherService, WatchListing
from .queries import FairPrice, Listing
from .settings import get_settings


class CatalogueRow(BaseModel):
    """A tracked reference with everything a catalogue view needs: curated
    attributes for filtering, live counts and prices for sorting. Prices are
    strings so a JSON round-trip can't quietly turn a decimal into a float."""

    ref: str
    brand: str
    model_name: str
    specs: Specs
    n_listings: int
    min_price: str | None
    median_price: str | None


server = MCPServer(
    name="assay-watch",
    title="assay-watch pricing",
    instructions=(
        "Read-only tools over a daily snapshot of luxury-watch listings from "
        "verified dealer storefronts. find_reference resolves free text to a "
        "tracked reference number; get_cheapest_listing and get_fair_price "
        "answer questions about a specific reference once you have one. "
        "Only USD listings are priced -- other currencies are counted but "
        "not converted."
    ),
)


@lru_cache
def _engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


@lru_cache
def _catalog() -> list[Reference]:
    return load_catalog(get_settings().references_path)


@lru_cache
def _fetcher() -> WatchFetcherService:
    """One instance for the process, so the cache and the rate limiter are
    shared across every request this instance serves. A per-call instance
    would reset both and make each of them decorative."""
    settings = get_settings()
    brands = tuple(sorted({r.brand for r in _catalog()}))
    return WatchFetcherService(
        api_key=settings.serpapi_key,
        known_brands=brands,
        cache=TTLCache(ttl_seconds=settings.live_search_ttl_seconds),
        min_interval_seconds=settings.live_search_min_interval,
    )


@server.tool()
def find_reference_tool(query: str) -> list[ReferenceMatch]:
    """Resolve free text (a nickname, brand, or partial reference number) to
    tracked watch references. Returns the best matches, most confident
    first, or an empty list if nothing in the tracked catalog matches --
    that's a legitimate answer, not an error."""
    return find_reference(query, _catalog())


@server.tool()
def list_tracked_references_tool() -> list[CatalogEntry]:
    """Every tracked reference (ref, brand, model name), unscored -- for a
    caller doing its own semantic matching (e.g. an LLM reasoning about a
    query like "steel dive watch under $15k" that find_reference's literal
    word-overlap can't resolve on its own)."""
    return [CatalogEntry(ref=r.ref, brand=r.brand, model_name=r.model_name) for r in _catalog()]


@server.tool()
def get_cheapest_listing_tool(reference: str) -> Listing | None:
    """The single lowest-priced current USD listing for a tracked reference,
    from the most recent crawl. Returns null if there's no current USD
    listing for this reference -- call find_reference first if unsure the
    reference is actually tracked."""
    settings = get_settings()
    return queries.get_cheapest_listing(_engine(settings.database_url), reference)


@server.tool()
def get_catalogue_overview_tool() -> list[CatalogueRow]:
    """Every tracked reference with its curated attributes (case size,
    movement, complication) and, where we have listings, how many and what
    they cost. One call, so a caller can render and filter a whole catalogue
    without a lookup per reference. Attributes come from hand-maintained
    config and may be incomplete; prices come from the latest crawl."""
    settings = get_settings()
    prices = {p.reference: p for p in queries.price_summary(_engine(settings.database_url))}
    rows: list[CatalogueRow] = []
    for ref in _catalog():
        p = prices.get(ref.ref)
        rows.append(
            CatalogueRow(
                ref=ref.ref,
                brand=ref.brand,
                model_name=ref.model_name,
                specs=ref.specs,
                n_listings=p.n_listings if p else 0,
                min_price=str(p.min_price) if p else None,
                median_price=str(p.median_price) if p else None,
            )
        )
    return rows


@server.tool()
def list_listings_tool(reference: str) -> list[Listing]:
    """Every current USD listing for a tracked reference, cheapest first --
    the listings the cheapest-price and fair-price answers are computed
    from. Use it to show the working behind those numbers, or to compare
    what individual dealers are asking. Implausibly priced rows (deposits,
    parts, accessories that carry the reference number) are already
    excluded. Returns an empty list if nothing current is on file."""
    settings = get_settings()
    return queries.list_listings(_engine(settings.database_url), reference)


@server.tool()
def get_fair_price_tool(reference: str) -> FairPrice | None:
    """A fair-price estimate for a tracked reference: the median (not
    average -- resists one outlier listing skewing it) of current USD
    listings, plus the range and how many listings it's based on. Returns
    null if there's no current USD listing for this reference."""
    settings = get_settings()
    return queries.get_fair_price(_engine(settings.database_url), reference)


@server.tool()
def search_live_listings_tool(
    query: str, source: ListingSource = "google_shopping"
) -> list[WatchListing]:
    """Live listings from a licensed search API, for a watch we have no
    crawled listings for. ``source`` is "google_shopping" for retail product
    results or "web_search" for general results.

    These are NOT dealer listings we collected and verified -- they are
    whatever a search API returned, and every row carries a ``source`` saying
    so. Never merge them with get_fair_price or get_cheapest_listing output,
    and never compute a fair price from them; use get_fair_price for that and
    present these separately. Returns an empty list if no live search is
    configured or the lookup failed, which is a normal answer, not an error.
    """
    return _fetcher().search(query, source)


def main() -> None:
    settings = get_settings()
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        stateless_http=True,  # Cloud Run may load-balance across instances
    )


if __name__ == "__main__":
    main()
