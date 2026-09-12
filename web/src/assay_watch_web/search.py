"""The actual product logic: query in, one honest answer out.

Deterministic first, AI only where it earns its place:
1. Ask the MCP server's deterministic word-overlap matcher. Free, instant,
   no LLM call needed for the common case (someone typing an actual
   reference number or exact model name).
2. Only if that's ambiguous (multiple candidates) or empty (no literal word
   overlap at all) does Gemini get involved -- resolving against either the
   ambiguous candidates or, if there were none, the full tracked catalog.
   It may come back with more than one, and that is an answer: a query like
   "GMT" describes several tracked watches equally well, so the buyer is
   offered the choice rather than handed an arbitrary one of them.
3. Once a reference is resolved, the pricing lookups and the final
   explanation are the only remaining steps -- never re-litigated by AI.
4. If we have no crawled listings for the resolved reference, and only
   then, a live web search fills the gap -- reported separately from
   crawled pricing, never merged into it.

Every step reports itself through ``on_stage`` so the UI can show what is
actually happening rather than a generic spinner. The stage id is a stable
machine identifier; the detail beside it is a finished sentence meant to be
displayed as-is, kept here rather than mapped in the front end so one step's
wording lives in one place. Neither is a claim about architecture: these are
sequential function calls, not agents deciding anything. The callback is
optional and never affects the result.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from . import gemini, mcp_client
from .gemini import WebMarketSnapshot
from .mcp_client import CatalogEntry, CheapestListing, FairPrice, WatchListing

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, str], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]
PartialCallback = Callable[["SearchResult"], Awaitable[None]]


class SearchResult(BaseModel):
    query: str
    resolved: CatalogEntry | None = None
    # Populated instead of ``resolved`` when the query matches several
    # tracked references equally well ("GMT"). Offering the choice is the
    # honest answer; picking one of them for the user is not.
    choices: list[CatalogEntry] = []
    cheapest: CheapestListing | None = None
    fair_price: FairPrice | None = None
    explanation: str | None = None
    web_market: WebMarketSnapshot | None = None
    # Live retail listings for a reference we have none of our own for.
    # A separate field from ``cheapest``/``fair_price`` because these are
    # asking prices off a search API, not dealer rows we collected -- the
    # type system is what keeps the two from being totalled together.
    live_listings: list[WatchListing] = []
    message: str | None = None


async def _noop_stage(stage: str, detail: str) -> None:
    return None


async def _noop_text(chunk: str) -> None:
    return None


async def _noop_partial(result: SearchResult) -> None:
    return None


async def _resolve(
    *,
    mcp_url: str,
    gemini_api_key: str,
    gemini_model: str,
    query: str,
    on_stage: StageCallback,
) -> list[CatalogEntry]:
    """The references that genuinely match. Empty means no match; more than
    one means the query was a category and the user should choose."""
    await on_stage("find_reference", "Identifying the reference")
    matches = await mcp_client.find_reference(mcp_url, query)

    exact = [m for m in matches if m.confidence == "exact"]
    if len(exact) == 1:
        await on_stage("resolved", f"Matched the {exact[0].brand} {exact[0].model_name}")
        return [exact[0]]
    if len(matches) == 1:
        await on_stage("resolved", f"Matched the {matches[0].brand} {matches[0].model_name}")
        return [matches[0]]

    candidates: list[CatalogEntry] = list(matches)
    if not candidates:
        await on_stage("list_tracked_references", "Reading the full tracked catalogue")
        candidates = await mcp_client.list_tracked_references(mcp_url)
    if not candidates:
        return []

    await on_stage("disambiguate", f"Narrowing down {len(candidates)} possible matches")
    pick = await gemini.resolve_reference(
        api_key=gemini_api_key, model=gemini_model, query=query, candidates=candidates
    )
    by_ref = {c.ref: c for c in candidates}
    chosen = [by_ref[r] for r in pick.refs if r in by_ref]
    if len(chosen) == 1:
        await on_stage("resolved", f"Matched the {chosen[0].brand} {chosen[0].model_name}")
    elif chosen:
        await on_stage("disambiguate", f"{len(chosen)} tracked references match equally")
    return chosen


async def run_search(
    query: str,
    *,
    mcp_url: str,
    gemini_api_key: str,
    gemini_model: str,
    on_stage: StageCallback | None = None,
    on_text: TextCallback | None = None,
    on_partial: PartialCallback | None = None,
) -> SearchResult:
    stage = on_stage or _noop_stage
    query = query.strip()
    if not query:
        return SearchResult(query=query, message="Type a watch model, brand, or reference number.")

    matched = await _resolve(
        mcp_url=mcp_url,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        query=query,
        on_stage=stage,
    )
    if not matched:
        return SearchResult(
            query=query,
            message="Couldn't match this to any of the watches this site tracks. "
            "Try a brand, model name, or reference number.",
        )
    if len(matched) > 1:
        await stage("done", "Done")
        return SearchResult(
            query=query,
            choices=matched,
            message=f"{len(matched)} of the watches we track match that. Which did you mean?",
        )

    resolved = matched[0]
    await stage("pricing", f"Checking dealer listings for {resolved.ref}")
    cheapest = await mcp_client.get_cheapest_listing(mcp_url, resolved.ref)
    fair = await mcp_client.get_fair_price(mcp_url, resolved.ref)

    if cheapest is None or fair is None:
        await stage("web_search", "No dealer listings on file \u2014 searching the live market")
        # Both live sources at once: they are independent lookups against
        # different providers, and running them in series would make the
        # emptiest result on the site also the slowest.
        web, live = await asyncio.gather(
            gemini.search_web_market(
                api_key=gemini_api_key, model=gemini_model, reference=resolved
            ),
            mcp_client.search_live_listings(
                mcp_url, f"{resolved.brand} {resolved.model_name} {resolved.ref}"
            ),
        )
        message = (
            f"No dealer listings for the {resolved.brand} {resolved.model_name} "
            f"({resolved.ref}) in our latest crawl."
            + ("" if web else " A live web search didn't turn up sourced pricing either.")
        )
        if web is None and not live:
            # Nothing grounded came back, so there is nothing to analyse.
            # Writing observations here would be the model talking about
            # figures that do not exist, which is the one thing it must not
            # do -- the honest dead end is better than an invented one.
            #
            # But a dead end should still say *why*. An unconfigured live
            # search, a model that would not run the search tool, and a
            # market with genuinely nothing on offer all render identically
            # otherwise, and they need completely different fixes -- which
            # has cost real time to work out by hand.
            try:
                status = await mcp_client.service_status(mcp_url)
                if not status.live_search_configured:
                    message += " Live retail lookup is not configured on this deployment."
            except Exception:
                logger.warning("could not read service status while explaining an empty result")
            await stage("done", "Done")
            return SearchResult(query=query, resolved=resolved, message=message)

        if web is None:
            # Retail listings but no grounded prose: still a real answer, and
            # the listings speak for themselves.
            await stage("done", "Done")
            return SearchResult(query=query, resolved=resolved, live_listings=live, message=message)

        # This branch used to end here, so a reference with no crawled
        # listings got prices and no reading of them -- exactly the case
        # where a buyer has least to go on and most needs one.
        await (on_partial or _noop_partial)(
            SearchResult(
                query=query,
                resolved=resolved,
                web_market=web,
                live_listings=live,
                message=message,
            )
        )
        await stage("explain", f"Weighing {len(web.sources)} web source(s)")
        web_text = on_text or _noop_text
        web_parts: list[str] = []
        async for chunk in gemini.stream_web_market_explanation(
            api_key=gemini_api_key, model=gemini_model, reference=resolved, web=web
        ):
            web_parts.append(chunk)
            await web_text(chunk)
        await stage("done", "Done")
        return SearchResult(
            query=query,
            resolved=resolved,
            web_market=web,
            live_listings=live,
            explanation="".join(web_parts).strip() or None,
            message=message,
        )

    # Hand over the numbers before asking for prose about them: they are
    # already final, and waiting on the explanation to show a price the user
    # came for would be holding finished work hostage to a garnish.
    await (on_partial or _noop_partial)(
        SearchResult(query=query, resolved=resolved, cheapest=cheapest, fair_price=fair)
    )

    await stage("explain", f"Weighing {fair.n_listings} current listings")
    # Streamed rather than awaited whole: the numbers are already on screen
    # by now, so the prose can arrive a word at a time instead of the page
    # sitting still until the last token lands. The assembled text still
    # comes back on the result, so a caller that ignores on_text loses
    # nothing.
    text = on_text or _noop_text
    parts: list[str] = []
    async for chunk in gemini.stream_fair_price_explanation(
        api_key=gemini_api_key, model=gemini_model, reference=resolved, cheapest=cheapest, fair=fair
    ):
        parts.append(chunk)
        await text(chunk)
    explanation = "".join(parts).strip()

    await stage("done", "Done")
    return SearchResult(
        query=query,
        resolved=resolved,
        cheapest=cheapest,
        fair_price=fair,
        explanation=explanation or None,
    )
