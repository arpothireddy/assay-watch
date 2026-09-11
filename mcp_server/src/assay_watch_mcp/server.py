"""The MCP server: wires the catalog matcher and pricing queries as tools.

Runs over streamable-http so it's reachable over the network (Cloud Run,
or any remote MCP client) rather than only over stdio.
"""

from __future__ import annotations

from functools import lru_cache

from mcp.server.mcpserver import MCPServer
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from . import queries
from .catalog import Reference, ReferenceMatch, find_reference, load_catalog
from .queries import CheapestListing, FairPrice
from .settings import get_settings

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


@server.tool()
def find_reference_tool(query: str) -> list[ReferenceMatch]:
    """Resolve free text (a nickname, brand, or partial reference number) to
    tracked watch references. Returns the best matches, most confident
    first, or an empty list if nothing in the tracked catalog matches --
    that's a legitimate answer, not an error."""
    return find_reference(query, _catalog())


@server.tool()
def get_cheapest_listing_tool(reference: str) -> CheapestListing | None:
    """The single lowest-priced current USD listing for a tracked reference,
    from the most recent crawl. Returns null if there's no current USD
    listing for this reference -- call find_reference first if unsure the
    reference is actually tracked."""
    settings = get_settings()
    return queries.get_cheapest_listing(_engine(settings.database_url), reference)


@server.tool()
def get_fair_price_tool(reference: str) -> FairPrice | None:
    """A fair-price estimate for a tracked reference: the median (not
    average -- resists one outlier listing skewing it) of current USD
    listings, plus the range and how many listings it's based on. Returns
    null if there's no current USD listing for this reference."""
    settings = get_settings()
    return queries.get_fair_price(_engine(settings.database_url), reference)


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
