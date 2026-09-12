"""Narrowly-scoped Gemini calls -- no chat, no agent loop. Each is a single
request/response:

- resolve_reference: fallback for when the MCP server's deterministic
  word-overlap matching can't confidently resolve a query (ambiguous
  candidates, or none at all) -- genuinely benefits from language
  understanding ("steel dive watch" -> Submariner).
- stream_fair_price_explanation: one short, factual sentence from real
  numbers already in hand, streamed as it is written. Not asked to invent
  anything -- just to phrase what the pricing tools already computed.
- search_web_market: the one call that does reach outside, via Gemini's
  Google Search grounding. Only runs when our own crawl has nothing for a
  resolved reference, and its output is kept provenance-separate from
  crawled data everywhere it surfaces -- see its docstring.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from google import genai
from google.genai import types
from pydantic import BaseModel

from .mcp_client import CatalogEntry, CheapestListing, FairPrice

logger = logging.getLogger(__name__)


class ResolvedPick(BaseModel):
    """Zero, one, or several references.

    Several is a real answer, not a failure: "GMT" describes three tracked
    watches equally well, and the model has no more information than the
    user does with which to choose between them. Forcing a single pick there
    either hides two of them or, when the model declines rather than guess,
    reports no match at all for something we plainly track.
    """

    refs: list[str]
    reasoning: str


def _catalog_text(candidates: list[CatalogEntry]) -> str:
    return "\n".join(f"{c.ref}: {c.brand} {c.model_name}" for c in candidates)


async def resolve_reference(
    *, api_key: str, model: str, query: str, candidates: list[CatalogEntry]
) -> ResolvedPick:
    """Resolve ``query`` to the tracked references that genuinely match it.

    Never forces a match onto an unrelated watch just because the list is
    short, and never breaks a tie it has no basis to break -- see
    ``ResolvedPick``.
    """
    prompt = (
        "You are matching a watch buyer's search query against a small "
        "curated list of tracked references.\n\n"
        "Return exactly one reference when the query identifies a specific "
        "watch, even loosely -- 'steel dive watch' is enough to mean a "
        "Submariner rather than a Daytona.\n"
        "Return several when the query names a category, family or "
        "complication that several references satisfy equally well, such as "
        "'GMT' or 'chronograph'. Do not pick arbitrarily between them; the "
        "buyer will choose.\n"
        "Return none when nothing on the list is a real match. Do not reach "
        "for the closest one.\n\n"
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
        return ResolvedPick(refs=[], reasoning="Model response could not be parsed.")
    # Anything the model names that isn't actually on the list is dropped
    # rather than trusted: the caller looks these up by exact reference.
    known = {c.ref for c in candidates}
    return ResolvedPick(refs=[r for r in parsed.refs if r in known], reasoning=parsed.reasoning)


def _explain_prompt(reference: CatalogEntry, cheapest: CheapestListing, fair: FairPrice) -> str:
    return (
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


async def stream_fair_price_explanation(
    *,
    api_key: str,
    model: str,
    reference: CatalogEntry,
    cheapest: CheapestListing,
    fair: FairPrice,
) -> AsyncIterator[str]:
    """The same explanation, yielded as the model produces it.

    Genuinely streamed rather than a typewriter replayed over finished text:
    the words appear when they are generated, so the pacing is the model's
    real pacing and the first words arrive sooner than the last ones would
    have. Falls back to nothing on failure -- the explanation is a garnish on
    numbers that are already on screen.
    """
    client = genai.Client(api_key=api_key)
    try:
        stream = await client.aio.models.generate_content_stream(
            model=model, contents=_explain_prompt(reference, cheapest, fair)
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text
    except Exception:
        logger.exception("streaming explanation failed for %s", reference.ref)


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
        logger.warning("web market search for %s came back with no text", reference.ref)
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
    # answer, so drop it. Logged rather than dropped quietly: from outside,
    # this looks identical to the call failing and to the search genuinely
    # finding nothing, and the three want completely different fixes.
    if not sources:
        logger.warning(
            "web market search for %s answered ungrounded (%d chunk(s), %d query(ies)) "
            "-- discarding; the model may not have run the search tool",
            reference.ref,
            len(chunks),
            len(queries),
        )
        return None

    logger.info(
        "web market search for %s grounded in %d source(s) via %d query(ies)",
        reference.ref,
        len(sources),
        len(queries),
    )

    return WebMarketSnapshot(summary=summary, sources=sources, queries=queries)
