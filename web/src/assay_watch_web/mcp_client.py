"""Thin async client for the assay-watch-mcp server's tools.

Deliberately defines its own minimal copies of the MCP server's result
shapes rather than depending on the assay-watch-mcp package -- that package
pulls in SQLAlchemy/psycopg for its own DB access, which this app has no use
for (it never touches Postgres directly, only through the MCP server). Same
decoupling principle the MCP server itself applies to the main crawler
package.

Opens a fresh connection per call rather than pooling one -- simplest thing
that works, and at this app's expected traffic the per-call handshake cost
is not worth the complexity of a persistent connection.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel


class Specs(BaseModel):
    """Curated, and every field optional -- see the MCP server's copy. A
    reference missing a field drops out of that filter rather than being
    bucketed under a guess."""

    case_mm: int | None = None
    movement: str | None = None
    category: str | None = None
    integrated_bracelet: bool | None = None


class CatalogEntry(BaseModel):
    ref: str
    brand: str
    model_name: str
    specs: Specs = Specs()


class CatalogueRow(CatalogEntry):
    n_listings: int
    min_price: str | None = None
    median_price: str | None = None


class ReferenceMatch(CatalogEntry):
    confidence: Literal["exact", "likely", "possible"]


class CheapestListing(BaseModel):
    seller_name: str | None
    price_amount: str
    price_currency: str
    raw_title: str
    url: str
    seen_at: str


class FairPrice(BaseModel):
    median_price: str
    min_price: str
    max_price: str
    n_listings: int
    excluded_other_currency: int
    excluded_implausible: int


class MCPError(Exception):
    """The MCP server reachable but a tool call itself failed."""


async def _call_tool(server_url: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    """Returns the tool's actual return value, already unwrapped.

    Confirmed live against a running server, not assumed: every tool's
    ``structured_content`` comes back as ``{"result": <value>}`` -- a list
    return, an object return, *and* a ``None`` return (an ``Optional[Model]``
    tool with nothing to report) are all wrapped the same way. A bare
    ``if data else None`` on the still-wrapped dict is a real bug here: an
    outer dict holding a null result is truthy, so it looked like data was
    present and blew up trying to build a model from ``{"result": None}``.
    """
    async with (
        streamable_http_client(server_url) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool_name, arguments)
        if result.is_error:
            raise MCPError(f"{tool_name} failed: {result.content}")
        return (result.structured_content or {}).get("result")


async def find_reference(server_url: str, query: str) -> list[ReferenceMatch]:
    data = await _call_tool(server_url, "find_reference_tool", {"query": query})
    return [ReferenceMatch(**m) for m in data or []]


async def list_tracked_references(server_url: str) -> list[CatalogEntry]:
    data = await _call_tool(server_url, "list_tracked_references_tool", {})
    return [CatalogEntry(**e) for e in data or []]


async def get_cheapest_listing(server_url: str, reference: str) -> CheapestListing | None:
    data = await _call_tool(server_url, "get_cheapest_listing_tool", {"reference": reference})
    return CheapestListing(**data) if data is not None else None


async def get_fair_price(server_url: str, reference: str) -> FairPrice | None:
    data = await _call_tool(server_url, "get_fair_price_tool", {"reference": reference})
    return FairPrice(**data) if data is not None else None


async def catalogue_overview(server_url: str) -> list[CatalogueRow]:
    data = await _call_tool(server_url, "get_catalogue_overview_tool", {})
    return [CatalogueRow(**row) for row in data or []]


async def list_listings(server_url: str, reference: str) -> list[CheapestListing]:
    """Every current listing behind the headline numbers, cheapest first.
    Same shape as the cheapest one because it is the same rows."""
    data = await _call_tool(server_url, "list_listings_tool", {"reference": reference})
    return [CheapestListing(**item) for item in data or []]
