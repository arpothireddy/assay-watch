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
import re
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
        + "\n\nWrite two or three short observations about what these numbers "
        "mean for someone deciding whether to buy: whether the cheapest "
        "listing looks like a genuine saving or a warning, how much weight "
        "the median carries given how many listings it rests on, and what "
        "the spread says about the market.\n\n"
        "One observation per line, each a single sentence, no bullet "
        "characters or numbering. Use only the numbers given -- don't invent "
        "anything, and don't restate them all, since they're already shown "
        "beside this."
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


class WebOffer(BaseModel):
    """One asking price a grounded search turned up.

    Deliberately not a ``Listing``: that is a row we fetched off a dealer's
    own storefront, this is a figure a language model read out of a search
    result. The price stays as text rather than a Decimal, because it is not
    a number we are entitled to compute with -- keeping it unparsed means it
    can never be quietly averaged into a median built from crawled data.
    """

    price_text: str
    merchant: str | None = None
    url: str
    condition: str | None = None


class ConditionGuide(BaseModel):
    """What a buyer should expect to pay for one configuration of the watch.

    Completeness moves the price of a used luxury watch more than almost
    anything else -- a full set with box and papers against a bare head is
    routinely a five-figure gap on the references tracked here -- so a single
    "market price" is the one answer a buyer cannot act on. Same provenance
    rules as WebOffer: read out of a grounded search, never computed, and
    kept out of every figure derived from crawled listings.
    """

    label: str
    price_text: str
    note: str | None = None


class WebMarketSnapshot(BaseModel):
    """What a live web search found. Deliberately a *different* type from
    FairPrice: that one is computed from listings we crawled ourselves off
    dealer sites we vetted, this one is a language model's reading of search
    results. Keeping them as separate types means neither the API nor the UI
    can accidentally present one as the other."""

    summary: str
    sources: list[WebSource]
    queries: list[str]
    offers: list[WebOffer] = []
    guidance: list[ConditionGuide] = []


class _Extracted(BaseModel):
    offers: list[WebOffer]
    guidance: list[ConditionGuide]


_DIGITS = re.compile(r"\d+")


def _price_is_supported(price_text: str, grounded_text: str) -> bool:
    """Whether ``price_text``'s figure actually occurs in the text the search
    produced.

    The extraction step runs without tools, so nothing stops it inventing a
    plausible number and attaching a real URL to it. This is the check that
    keeps that from shipping: the digits are stripped out of both sides and
    the price must appear in the grounded text, so "$13,000", "13,000 USD"
    and "13000" all agree while a figure that was never on the page does not.
    """
    price_digits = "".join(_DIGITS.findall(price_text))
    if not price_digits:
        return False
    return price_digits in "".join(_DIGITS.findall(grounded_text))


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
        "Cover, in a few short factual sentences:\n"
        "- the typical asking-price range you found;\n"
        "- what the same watch goes for as a full set with box and papers "
        "versus as the watch alone, since that gap is usually the single "
        "biggest thing a buyer can act on;\n"
        "- what unworn or new examples ask against pre-owned ones;\n"
        "- anything notable about current availability.\n\n"
        "State the actual figures you found for each of those, not just that "
        "a difference exists. Attribute them to what the search results say. "
        "If the results disagree or are thin, say so plainly rather than "
        "settling on a confident number."
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

    # Only now, once the answer is known to be grounded, is it worth a second
    # call to pull the individual asking prices out of it.
    offers, guidance = await _extract_offers(
        client=client, model=model, reference=reference, grounded_text=summary, sources=sources
    )

    logger.info(
        "web market search for %s grounded in %d source(s) via %d query(ies), "
        "%d offer(s) and %d guidance row(s) kept",
        reference.ref,
        len(sources),
        len(queries),
        len(offers),
        len(guidance),
    )

    return WebMarketSnapshot(
        summary=summary, sources=sources, queries=queries, offers=offers, guidance=guidance
    )


async def _extract_offers(
    *,
    client: genai.Client,
    model: str,
    reference: CatalogEntry,
    grounded_text: str,
    sources: list[WebSource],
) -> tuple[list[WebOffer], list[ConditionGuide]]:
    """Pull the individual asking prices, and the what-to-pay guidance, out
    of a grounded answer.

    A second call rather than a schema on the first one: structured output
    and the search tool do not reliably coexist in one request, and splitting
    them means this step runs with no tools at all, where a schema is safe.

    It is also the untrusted step. It sees only text the grounded search
    already produced, and everything it returns is checked back against that
    text and against the URLs the search actually cited -- an offer whose
    price was never on the page, or whose link the search never returned, is
    dropped rather than shown. The model cannot introduce a price or a
    merchant here; it can only structure ones that survived grounding.
    """
    known_urls = {s.url for s in sources}
    prompt = (
        "Below is the result of a web search for asking prices on a "
        f"{reference.brand} {reference.model_name} (ref. {reference.ref}), "
        "followed by the pages it came from.\n\n"
        "Return two things.\n\n"
        "offers: each distinct asking price the text states for a specific "
        "listing. Copy the price exactly as written, including its currency "
        "symbol. Attach each to the page URL it came from, chosen from the "
        "list below. Note the condition only if the text says so.\n\n"
        "guidance: what a buyer should expect to pay for each configuration "
        "the text gives a figure for -- full set with box and papers, watch "
        "only, unworn, pre-owned. Label each one in those words, give the "
        "price as written, and add a short note only if the text explains "
        "the difference. Omit any configuration the text does not price.\n\n"
        "Do not calculate, convert, average or estimate any price, and do "
        "not include a price the text does not state. If it gives only a "
        "range with no individual prices, return no offers.\n\n"
        f"Search result:\n{grounded_text}\n\n"
        "Pages:\n" + "\n".join(f"{s.url} ({s.domain or s.title})" for s in sources)
    )
    try:
        resp = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=_Extracted
            ),
        )
    except Exception:
        logger.exception("offer extraction failed for %s", reference.ref)
        return [], []

    parsed = resp.parsed
    if not isinstance(parsed, _Extracted):
        logger.warning("offer extraction for %s could not be parsed", reference.ref)
        return [], []

    kept: list[WebOffer] = []
    for offer in parsed.offers:
        if offer.url not in known_urls:
            logger.warning(
                "dropping offer for %s citing %r, which the search did not return",
                reference.ref,
                offer.url,
            )
            continue
        if not _price_is_supported(offer.price_text, grounded_text):
            logger.warning(
                "dropping offer for %s priced %r, which is not in the grounded text",
                reference.ref,
                offer.price_text,
            )
            continue
        kept.append(offer)

    # Guidance carries no URL to check, so the price check is the only thing
    # standing between a buyer and an invented number. It is not optional.
    guide: list[ConditionGuide] = []
    for row in parsed.guidance:
        if not _price_is_supported(row.price_text, grounded_text):
            logger.warning(
                "dropping %r guidance for %s priced %r, which is not in the grounded text",
                row.label,
                reference.ref,
                row.price_text,
            )
            continue
        guide.append(row)
    return kept, guide
