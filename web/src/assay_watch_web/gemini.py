"""Two narrowly-scoped Gemini calls -- no chat, no agent loop, no tool-calling
from the model itself. Each is a single request/response with structured
output:

- resolve_reference: fallback for when the MCP server's deterministic
  word-overlap matching can't confidently resolve a query (ambiguous
  candidates, or none at all) -- genuinely benefits from language
  understanding ("steel dive watch" -> Submariner).
- explain_fair_price: one short, factual sentence from real numbers already
  in hand. Not asked to invent anything -- just to phrase what the pricing
  tools already computed.
"""

from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

from .mcp_client import CatalogEntry, CheapestListing, FairPrice


class ResolvedPick(BaseModel):
    ref: str | None
    reasoning: str


def _catalog_text(candidates: list[CatalogEntry]) -> str:
    return "\n".join(f"{c.ref}: {c.brand} {c.model_name}" for c in candidates)


async def resolve_reference(
    *, api_key: str, model: str, query: str, candidates: list[CatalogEntry]
) -> ResolvedPick:
    """Pick the single best-matching tracked reference for ``query`` from
    ``candidates``, or ``ref=None`` if nothing genuinely matches. Never
    forces a match onto an unrelated watch just because the list is short."""
    prompt = (
        "You are matching a watch buyer's search query to one specific "
        "tracked reference from a small curated list. Only pick a reference "
        "if it genuinely matches what the buyer described -- if nothing on "
        "the list is a real match, say so with ref=null rather than "
        "guessing the closest one.\n\n"
        f"Tracked references:\n{_catalog_text(candidates)}\n\n"
        f"Buyer's query: {query!r}"
    )
    client = genai.Client(api_key=api_key)
    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=ResolvedPick
        ),
    )
    parsed = resp.parsed
    if not isinstance(parsed, ResolvedPick):
        return ResolvedPick(ref=None, reasoning="Model response could not be parsed.")
    return parsed


async def explain_fair_price(
    *,
    api_key: str,
    model: str,
    reference: CatalogEntry,
    cheapest: CheapestListing,
    fair: FairPrice,
) -> str:
    """One short, plain-English sentence explaining the fair-price number --
    phrasing real numbers already computed, not inventing new ones."""
    prompt = (
        f"A buyer is looking at a {reference.brand} {reference.model_name} "
        f"(ref. {reference.ref}). The cheapest current listing is "
        f"${cheapest.price_amount} at {cheapest.seller_name or 'a dealer'}. "
        f"Across {fair.n_listings} current listings, the median price is "
        f"${fair.median_price} (range ${fair.min_price}-${fair.max_price})."
        + (
            f" {fair.excluded_other_currency} additional non-USD listing(s) "
            "were found but aren't included in these numbers."
            if fair.excluded_other_currency
            else ""
        )
        + "\n\nWrite one or two short, factual sentences explaining why the "
        "median is a reasonable 'fair price' reference point. Use only the "
        "numbers given -- don't invent anything, and don't repeat all the "
        "numbers verbatim since they're already shown elsewhere on the page."
    )
    client = genai.Client(api_key=api_key)
    resp = await client.aio.models.generate_content(model=model, contents=prompt)
    return (resp.text or "").strip()
