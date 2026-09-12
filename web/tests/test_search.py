"""Orchestration tests. mcp_client and gemini are monkeypatched here --
their own real behavior is each tested independently (mcp_server has its
own suite; a live Gemini call isn't something a unit test should make)."""

from __future__ import annotations

import pytest

from assay_watch_web import gemini, mcp_client, search
from assay_watch_web.gemini import ResolvedPick, WebMarketSnapshot, WebSource
from assay_watch_web.mcp_client import CatalogEntry, CheapestListing, FairPrice, ReferenceMatch

_SUB = CatalogEntry(ref="126610LN", brand="Rolex", model_name="Submariner Date")
_DAYTONA = CatalogEntry(ref="116500LN", brand="Rolex", model_name="Daytona")

_CHEAPEST = CheapestListing(
    seller_name="dealer",
    price_amount="12500.00",
    price_currency="USD",
    raw_title="Rolex Submariner",
    url="https://dealer.test/x",
    seen_at="2026-01-01T00:00:00Z",
)
_FAIR = FairPrice(
    median_price="13000.00",
    min_price="12500.00",
    max_price="14000.00",
    n_listings=3,
    excluded_other_currency=0,
)
_WEB = WebMarketSnapshot(
    summary="Asking prices cluster around $13k on the grey market.",
    sources=[
        WebSource(title="Example listing", url="https://example.test/x", domain="example.test")
    ],
    queries=["rolex submariner 126610LN price"],
)


async def _returns(value: object) -> object:
    """Helper so a monkeypatched lambda can stand in for an async function:
    ``lambda *a, **k: _returns(x)`` returns a coroutine, which is exactly
    what ``await mcp_client.some_call(...)`` needs -- a plain lambda
    returning ``x`` directly would break the ``await`` in search.py."""
    return value


async def test_empty_query_short_circuits_with_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("should not be called for an empty query")

    monkeypatch.setattr(mcp_client, "find_reference", fail)

    result = await search.run_search(
        "   ", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is None
    assert result.message


async def test_single_exact_match_skips_gemini_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    async def fail_resolve(*args: object, **kwargs: object) -> ResolvedPick:
        raise AssertionError("Gemini matching must not run for a single exact match")

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(_CHEAPEST))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))
    monkeypatch.setattr(gemini, "resolve_reference", fail_resolve)
    monkeypatch.setattr(gemini, "explain_fair_price", lambda *a, **k: _returns("Looks fair."))

    result = await search.run_search(
        "126610LN", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is not None
    assert result.resolved.ref == "126610LN"
    assert result.cheapest == _CHEAPEST
    assert result.fair_price == _FAIR
    assert result.explanation == "Looks fair."


async def test_ambiguous_matches_are_disambiguated_by_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [
            ReferenceMatch(**_SUB.model_dump(), confidence="possible"),
            ReferenceMatch(**_DAYTONA.model_dump(), confidence="possible"),
        ]

    async def resolve_reference(**kwargs: object) -> ResolvedPick:
        assert len(kwargs["candidates"]) == 2  # type: ignore[arg-type]
        return ResolvedPick(ref="126610LN", reasoning="dive watch, not a chronograph")

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(_CHEAPEST))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))
    monkeypatch.setattr(gemini, "resolve_reference", resolve_reference)
    monkeypatch.setattr(gemini, "explain_fair_price", lambda *a, **k: _returns("Looks fair."))

    result = await search.run_search(
        "black rolex dive watch", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is not None
    assert result.resolved.ref == "126610LN"


async def test_no_word_overlap_falls_back_to_full_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return []

    async def list_tracked_references(url: str) -> list[CatalogEntry]:
        return [_SUB, _DAYTONA]

    async def resolve_reference(**kwargs: object) -> ResolvedPick:
        assert len(kwargs["candidates"]) == 2  # type: ignore[arg-type]
        return ResolvedPick(ref="126610LN", reasoning="steel dive watch matches the Submariner")

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "list_tracked_references", list_tracked_references)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(_CHEAPEST))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))
    monkeypatch.setattr(gemini, "resolve_reference", resolve_reference)
    monkeypatch.setattr(gemini, "explain_fair_price", lambda *a, **k: _returns("Looks fair."))

    result = await search.run_search(
        "steel dive watch under 15k", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is not None
    assert result.resolved.ref == "126610LN"


async def test_gemini_declining_to_pick_is_a_clean_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [
            ReferenceMatch(**_SUB.model_dump(), confidence="possible"),
            ReferenceMatch(**_DAYTONA.model_dump(), confidence="possible"),
        ]

    async def resolve_reference(**kwargs: object) -> ResolvedPick:
        return ResolvedPick(ref=None, reasoning="a Casio doesn't match either candidate")

    async def fail_pricing(*args: object, **kwargs: object) -> None:
        raise AssertionError("pricing must not be looked up when nothing resolved")

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(gemini, "resolve_reference", resolve_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", fail_pricing)

    result = await search.run_search(
        "a Casio", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is None
    assert result.message


async def test_no_crawled_listings_falls_back_to_a_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    async def fail_explain(*args: object, **kwargs: object) -> str:
        raise AssertionError("must not ask for an explanation with no pricing data")

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(None))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(None))
    monkeypatch.setattr(gemini, "explain_fair_price", fail_explain)
    monkeypatch.setattr(gemini, "search_web_market", lambda *a, **k: _returns(_WEB))

    result = await search.run_search(
        "126610LN", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is not None
    assert result.cheapest is None
    assert result.fair_price is None
    # The web snapshot rides in its own field -- never folded into
    # fair_price, which means "computed from listings we crawled ourselves".
    assert result.web_market == _WEB
    assert result.message and "no dealer listings" in result.message.lower()


async def test_web_search_returning_nothing_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(None))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(None))
    monkeypatch.setattr(gemini, "search_web_market", lambda *a, **k: _returns(None))

    result = await search.run_search(
        "126610LN", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.resolved is not None
    assert result.web_market is None
    assert result.message and "web search" in result.message.lower()


async def test_web_search_is_skipped_when_crawled_pricing_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paid-for, slower, less-trusted path must not run when we already
    have our own data -- otherwise every search costs a web search."""

    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    async def fail_web(*args: object, **kwargs: object) -> None:
        raise AssertionError("web search must not run when crawled pricing is present")

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(_CHEAPEST))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))
    monkeypatch.setattr(gemini, "explain_fair_price", lambda *a, **k: _returns("Looks fair."))
    monkeypatch.setattr(gemini, "search_web_market", fail_web)

    result = await search.run_search(
        "126610LN", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.web_market is None
    assert result.fair_price == _FAIR


async def test_stage_callback_reports_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(_CHEAPEST))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))
    monkeypatch.setattr(gemini, "explain_fair_price", lambda *a, **k: _returns("Looks fair."))

    seen: list[str] = []

    async def on_stage(stage: str, detail: str) -> None:
        seen.append(stage)

    await search.run_search(
        "126610LN",
        mcp_url="http://mcp",
        gemini_api_key="k",
        gemini_model="m",
        on_stage=on_stage,
    )
    assert seen[0] == "find_reference"
    assert "pricing" in seen
    assert seen[-1] == "done"


async def test_search_works_without_a_stage_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_stage is optional -- /api/search passes none."""

    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_cheapest_listing", lambda *a, **k: _returns(_CHEAPEST))
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))
    monkeypatch.setattr(gemini, "explain_fair_price", lambda *a, **k: _returns("Looks fair."))

    result = await search.run_search(
        "126610LN", mcp_url="http://mcp", gemini_api_key="k", gemini_model="m"
    )
    assert result.cheapest == _CHEAPEST
