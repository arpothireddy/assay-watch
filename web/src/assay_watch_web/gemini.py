"""Narrowly-scoped Gemini calls -- no chat, no agent loop. Each is a single
request/response:

- resolve_reference: fallback for when the MCP server's deterministic
  word-overlap matching can't confidently resolve a query (ambiguous
  candidates, or none at all) -- genuinely benefits from language
  understanding ("steel dive watch" -> Submariner).
- explain_fair_price: one short, factual sentence from real numbers already
  in hand. Not asked to invent anything -- just to phrase what the pricing
  tools already computed.
- search_web_market: the one call that does reach outside, via Gemini's
  Google Search grounding. Only runs when our own crawl has nothing for a
  resolved reference, and its output is kept provenance-separate from
  crawled data everywhere it surfaces -- see its docstring.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types
from pydantic import BaseModel

from .mcp_client import CatalogEntry, CheapestListing, FairPrice

logger = logging.getLogger(__name__)


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


class WebSource(BaseModel):
    title: str
    url: str
    domain: str | None = None


class WebMarketSnapshot(BaseModel):
    """What a live web search found. Deliberately a *different* type from
    FairPrice: that one is computed from listings we crawled ourselves off
    dealer sites we vetted, this one is a language model's reading of search
    results. Keeping them as separate types means neither the API nor the UI
    can accidentally present one as the other."""

    summary: str
    sources: list[WebSource]
    queries: list[str]


async def search_web_market(
    *, api_key: str, model: str, reference: CatalogEntry
) -> WebMarketSnapshot | None:
    """Ask Gemini, with Google Search grounding, what this reference is
    currently asking on the open market.

    This is the fallback for a reference we track but have no crawled
    listings for -- which is the common case, since the dealers we crawl
    stock a narrower slice of the market than the references people search
    for. Returns ``None`` rather than raising if the call fails or comes
    back ungrounded: a missing extra is not worth failing the whole search
    over, and an *ungrounded* answer here would be exactly the model
    guessing prices from memory, which is the one thing this must not do.
    """
    prompt = (
        f"Search for what a {reference.brand} {reference.model_name} "
        f"(reference {reference.ref}) is currently selling for on the "
        "pre-owned and grey market.\n\n"
        "Write two or three short, factual sentences covering the typical "
        "asking-price range you found and anything notable about current "
        "availability. Attribute figures to what the search results actually "
        "say. If the results disagree or are thin, say so plainly rather "
        "than settling on a confident number."
    )
    client = genai.Client(api_key=api_key)
    try:
        resp = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
    except Exception:
        logger.exception("web market search failed for %s", reference.ref)
        return None

    summary = (resp.text or "").strip()
    if not summary:
        return None

    candidates = resp.candidates or []
    meta = candidates[0].grounding_metadata if candidates else None
    chunks = (meta.grounding_chunks or []) if meta else []
    queries = list((meta.web_search_queries or []) if meta else [])

    sources: list[WebSource] = []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if web is None or not web.uri:
            continue
        sources.append(
            WebSource(title=web.title or web.uri, url=web.uri, domain=getattr(web, "domain", None))
        )

    # No grounding chunks means the model answered without actually
    # consulting search results -- unsourced price claims are worse than no
    # answer, so drop it.
    if not sources:
        return None

    return WebMarketSnapshot(summary=summary, sources=sources, queries=queries)
