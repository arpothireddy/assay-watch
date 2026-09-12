"""The orchestrator: a model that is given the MCP tools and decides.

This is the layer the architecture was missing. Until now every MCP tool was
invoked by our own Python, by name, with arguments our code assembled -- a
remote procedure call in a protocol built so that a *model* could choose.
Here the model gets the tool list and sequences its own lookups.

It deliberately does not replace the deterministic path. ``search.py`` still
answers an exact reference in one round trip with no model call at all, and
that is the common case. The orchestrator is for the queries that path
cannot serve: a category, a budget, a comparison, a watch we do not track --
the ones where knowing *which* lookups to do is the actual work.

The division of labour that makes this safe:

- Every figure the orchestrator reports is computed by a tool, in SQL, in
  the deterministic core. The model chooses which questions to ask; it never
  computes an answer.
- Tool results are data, not instructions. Listing titles and merchant names
  come from dealer storefronts and a search API, so text inside them is
  never treated as direction.
- The loop is bounded. A model that keeps calling tools without concluding
  is stopped and its partial work reported, rather than running until the
  request times out.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google import genai
from google.genai import types

from . import mcp_client

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, str], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]

# Enough turns for: resolve, price, inspect listings, maybe a live lookup,
# then answer. Past this the model is looping rather than working, and the
# right move is to report what it has instead of spending more of the user's
# patience and our quota.
MAX_TOOL_TURNS = 8

_SYSTEM = """You answer questions about luxury watch prices for a buyer.

You have tools over two different bodies of data, and the difference matters
more than anything else you do:

- find_reference, get_fair_price, get_cheapest_listing, list_listings and
  get_catalogue_overview read listings we crawled ourselves from dealer
  storefronts we vetted. These are our own verified data. Fair prices come
  only from here.
- search_live_listings reads a licensed search API. It is what other people
  are currently *asking*, not a price anyone verified or paid. Use it when we
  have no crawled listings for what the buyer wants, and say plainly where it
  came from.

Never present the two as the same thing, never compute an average or a fair
price across them, and never state a figure a tool did not return. If the
tools cannot answer, say so -- that is a real answer and a useful one.

Work by calling tools, then write two or three short observations for the
buyer, one per line, no bullets or numbering. Be concrete about the numbers
and honest about how thin the evidence is."""


def _declarations() -> list[types.FunctionDeclaration]:
    """The MCP tool surface, as function declarations the model can call.

    Hand-declared rather than discovered at runtime on purpose: the
    descriptions here are what the model steers on, and they carry the
    provenance rules that the tools' own docstrings state for a human reader.
    A tool the orchestrator should not drive is simply left out of this list.
    """
    return [
        types.FunctionDeclaration(
            name="find_reference",
            description=(
                "Resolve free text (a nickname, brand, or partial reference number) to "
                "tracked watch references. Returns the best matches, most confident "
                "first, or an empty list if nothing tracked matches -- which is a real "
                "answer, not an error. Call this first for any specific watch."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"query": types.Schema(type=types.Type.STRING)},
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_fair_price",
            description=(
                "The fair-price estimate for one tracked reference: the median of "
                "current USD dealer listings we crawled, with the range and how many "
                "listings it rests on. This is the only source of a fair price. Returns "
                "null when we have no crawled listings for it."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"reference": types.Schema(type=types.Type.STRING)},
                required=["reference"],
            ),
        ),
        types.FunctionDeclaration(
            name="list_listings",
            description=(
                "Every current USD dealer listing behind a reference's numbers, "
                "cheapest first. Use it to show the working, or to compare what "
                "individual dealers ask. Implausible rows (deposits, parts) are "
                "already excluded."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"reference": types.Schema(type=types.Type.STRING)},
                required=["reference"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_catalogue_overview",
            description=(
                "Every tracked reference with its curated attributes (case size, "
                "movement, complication) and, where we have them, listing counts and "
                "prices. One call. Use it for browsing questions -- a budget, a "
                "category, a comparison -- rather than one specific watch."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="search_live_listings",
            description=(
                "Live listings from a licensed search API for a watch we have no "
                "crawled listings for. These are asking prices from the open web, NOT "
                "dealer listings we verified: never merge them with crawled data and "
                "never compute a fair price from them. source is 'google_shopping' for "
                "retail product results or 'web_search' for general results. An empty "
                "list means no live search is configured or none was found."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "query": types.Schema(type=types.Type.STRING),
                    "source": types.Schema(
                        type=types.Type.STRING, enum=["google_shopping", "web_search"]
                    ),
                },
                required=["query"],
            ),
        ),
    ]


async def _dispatch(mcp_url: str, name: str, args: dict[str, Any]) -> Any:
    """Run one tool call against the MCP server.

    Errors are returned to the model rather than raised: a tool that fails is
    something it can route around (try a different reference, fall back to a
    live search), where an exception here would lose the turns already spent.
    """
    try:
        if name == "find_reference":
            return [
                m.model_dump(mode="json")
                for m in await mcp_client.find_reference(mcp_url, str(args.get("query", "")))
            ]
        if name == "get_fair_price":
            fair = await mcp_client.get_fair_price(mcp_url, str(args.get("reference", "")))
            return fair.model_dump(mode="json") if fair else None
        if name == "list_listings":
            return [
                x.model_dump(mode="json")
                for x in await mcp_client.list_listings(mcp_url, str(args.get("reference", "")))
            ]
        if name == "get_catalogue_overview":
            return [x.model_dump(mode="json") for x in await mcp_client.catalogue_overview(mcp_url)]
        if name == "search_live_listings":
            return [
                x.model_dump(mode="json")
                for x in await mcp_client.search_live_listings(
                    mcp_url,
                    str(args.get("query", "")),
                    str(args.get("source") or "google_shopping"),
                )
            ]
    except Exception as exc:
        logger.exception("orchestrator tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown tool {name}"}


class OrchestratorResult:
    """What the run produced, and what it did to get there."""

    def __init__(self, text: str, calls: list[str], truncated: bool) -> None:
        self.text = text
        self.calls = calls
        self.truncated = truncated


async def run(
    query: str,
    *,
    mcp_url: str,
    api_key: str,
    model: str,
    on_stage: StageCallback | None = None,
    on_text: TextCallback | None = None,
) -> OrchestratorResult:
    """Let the model answer ``query`` by choosing its own lookups."""
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        tools=[types.Tool(function_declarations=_declarations())],
    )
    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part(text=query)])]
    calls: list[str] = []

    for _turn in range(MAX_TOOL_TURNS):
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception:
            logger.exception("orchestrator generation failed for %r", query)
            return OrchestratorResult("", calls, truncated=True)

        candidates = response.candidates or []
        parts = (
            list(candidates[0].content.parts or []) if candidates and candidates[0].content else []
        )
        requested = [p.function_call for p in parts if p.function_call]

        if not requested:
            text = (response.text or "").strip()
            if on_text and text:
                await on_text(text)
            return OrchestratorResult(text, calls, truncated=False)

        contents.append(types.Content(role="model", parts=parts))
        results: list[types.Part] = []
        for call in requested:
            name = call.name or ""
            args = dict(call.args or {})
            calls.append(name)
            if on_stage:
                await on_stage(name, _describe(name, args))
            value = await _dispatch(mcp_url, name, args)
            results.append(
                types.Part.from_function_response(
                    name=name,
                    # Wrapped in a dict because the API requires an object
                    # response, and json round-tripped so Decimals and dates
                    # from the tools survive as strings the model can read.
                    response={"result": json.loads(json.dumps(value, default=str))},
                )
            )
        contents.append(types.Content(role="user", parts=results))

    logger.warning("orchestrator hit the turn limit for %r after %s", query, calls)
    return OrchestratorResult("", calls, truncated=True)


def _describe(name: str, args: dict[str, Any]) -> str:
    """A finished sentence for the progress line, so the UI can show what the
    model actually chose to do rather than a generic spinner."""
    if name == "find_reference":
        return f"Identifying “{args.get('query', '')}”"
    if name == "get_fair_price":
        return f"Pricing {args.get('reference', '')} against dealer listings"
    if name == "list_listings":
        return f"Reading every listing for {args.get('reference', '')}"
    if name == "get_catalogue_overview":
        return "Reading the tracked catalogue"
    if name == "search_live_listings":
        return f"Searching the live market for “{args.get('query', '')}”"
    return name
