"""The actual product logic: query in, one honest answer out.

Deterministic first, AI only where it earns its place:
1. Ask the MCP server's deterministic word-overlap matcher. Free, instant,
   no LLM call needed for the common case (someone typing an actual
   reference number or exact model name).
2. Only if that's ambiguous (multiple candidates) or empty (no literal word
   overlap at all) does Gemini get involved -- resolving against either the
   ambiguous candidates or, if there were none, the full tracked catalog.
3. Once a reference is resolved, the pricing lookups and the final
   explanation are the only remaining steps -- never re-litigated by AI.
4. If we have no crawled listings for the resolved reference, and only
   then, a live web search fills the gap -- reported separately from
   crawled pricing, never merged into it.

Every step reports itself through ``on_stage`` so the UI can show what the
pipeline is actually doing rather than a generic spinner. The callback is
optional and never affects the result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from . import gemini, mcp_client
from .gemini import WebMarketSnapshot
from .mcp_client import CatalogEntry, CheapestListing, FairPrice

StageCallback = Callable[[str, str], Awaitable[None]]


class SearchResult(BaseModel):
    query: str
    resolved: CatalogEntry | None = None
    cheapest: CheapestListing | None = None
    fair_price: FairPrice | None = None
    explanation: str | None = None
    web_market: WebMarketSnapshot | None = None
    message: str | None = None


async def _noop_stage(stage: str, detail: str) -> None:
    return None


async def _resolve(
    *,
    mcp_url: str,
    gemini_api_key: str,
    gemini_model: str,
    query: str,
    on_stage: StageCallback,
) -> CatalogEntry | None:
    await on_stage("find_reference", "matching against the tracked catalogue")
    matches = await mcp_client.find_reference(mcp_url, query)

    exact = [m for m in matches if m.confidence == "exact"]
    if len(exact) == 1:
        await on_stage("resolved", f"exact match: {exact[0].ref}")
        return exact[0]
    if len(matches) == 1:
        await on_stage("resolved", f"single match: {matches[0].ref}")
        return matches[0]

    candidates: list[CatalogEntry] = list(matches)
    if not candidates:
        await on_stage("list_tracked_references", "no word overlap; loading full catalogue")
        candidates = await mcp_client.list_tracked_references(mcp_url)
    if not candidates:
        return None

    await on_stage("disambiguate", f"asking the model to choose among {len(candidates)}")
    pick = await gemini.resolve_reference(
        api_key=gemini_api_key, model=gemini_model, query=query, candidates=candidates
    )
    if pick.ref is None:
        return None
    resolved = next((c for c in candidates if c.ref == pick.ref), None)
    if resolved is not None:
        await on_stage("resolved", f"model chose {resolved.ref}")
    return resolved


async def run_search(
    query: str,
    *,
    mcp_url: str,
    gemini_api_key: str,
    gemini_model: str,
    on_stage: StageCallback | None = None,
) -> SearchResult:
    stage = on_stage or _noop_stage
    query = query.strip()
    if not query:
        return SearchResult(query=query, message="Type a watch model, brand, or reference number.")

    resolved = await _resolve(
        mcp_url=mcp_url,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        query=query,
        on_stage=stage,
    )
    if resolved is None:
        return SearchResult(
            query=query,
            message="Couldn't match this to any of the watches this site tracks. "
            "Try a brand, model name, or reference number.",
        )

    await stage("pricing", f"querying crawled listings for {resolved.ref}")
    cheapest = await mcp_client.get_cheapest_listing(mcp_url, resolved.ref)
    fair = await mcp_client.get_fair_price(mcp_url, resolved.ref)

    if cheapest is None or fair is None:
        await stage("web_search", "no crawled listings; searching the live web")
        web = await gemini.search_web_market(
            api_key=gemini_api_key, model=gemini_model, reference=resolved
        )
        await stage("done", "complete")
        return SearchResult(
            query=query,
            resolved=resolved,
            web_market=web,
            message=f"No dealer listings for the {resolved.brand} {resolved.model_name} "
            f"({resolved.ref}) in our latest crawl."
            + ("" if web else " A live web search didn't turn up sourced pricing either."),
        )

    await stage("explain", f"{fair.n_listings} listings; composing the fair-price read")
    explanation = await gemini.explain_fair_price(
        api_key=gemini_api_key, model=gemini_model, reference=resolved, cheapest=cheapest, fair=fair
    )

    await stage("done", "complete")
    return SearchResult(
        query=query, resolved=resolved, cheapest=cheapest, fair_price=fair, explanation=explanation
    )
