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
"""

from __future__ import annotations

from pydantic import BaseModel

from . import gemini, mcp_client
from .mcp_client import CatalogEntry, CheapestListing, FairPrice


class SearchResult(BaseModel):
    query: str
    resolved: CatalogEntry | None = None
    cheapest: CheapestListing | None = None
    fair_price: FairPrice | None = None
    explanation: str | None = None
    message: str | None = None


async def _resolve(
    *, mcp_url: str, gemini_api_key: str, gemini_model: str, query: str
) -> CatalogEntry | None:
    matches = await mcp_client.find_reference(mcp_url, query)

    exact = [m for m in matches if m.confidence == "exact"]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]

    candidates: list[CatalogEntry] = list(matches)
    if not candidates:
        candidates = await mcp_client.list_tracked_references(mcp_url)
    if not candidates:
        return None

    pick = await gemini.resolve_reference(
        api_key=gemini_api_key, model=gemini_model, query=query, candidates=candidates
    )
    if pick.ref is None:
        return None
    return next((c for c in candidates if c.ref == pick.ref), None)


async def run_search(
    query: str, *, mcp_url: str, gemini_api_key: str, gemini_model: str
) -> SearchResult:
    query = query.strip()
    if not query:
        return SearchResult(query=query, message="Type a watch model, brand, or reference number.")

    resolved = await _resolve(
        mcp_url=mcp_url, gemini_api_key=gemini_api_key, gemini_model=gemini_model, query=query
    )
    if resolved is None:
        return SearchResult(
            query=query,
            message="Couldn't match this to any of the watches this site tracks. "
            "Try a brand, model name, or reference number.",
        )

    cheapest = await mcp_client.get_cheapest_listing(mcp_url, resolved.ref)
    fair = await mcp_client.get_fair_price(mcp_url, resolved.ref)

    if cheapest is None or fair is None:
        return SearchResult(
            query=query,
            resolved=resolved,
            message=f"Found the {resolved.brand} {resolved.model_name} ({resolved.ref}), "
            "but there are no current USD listings for it right now.",
        )

    explanation = await gemini.explain_fair_price(
        api_key=gemini_api_key, model=gemini_model, reference=resolved, cheapest=cheapest, fair=fair
    )

    return SearchResult(
        query=query, resolved=resolved, cheapest=cheapest, fair_price=fair, explanation=explanation
    )
