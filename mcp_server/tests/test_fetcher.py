"""Tests for the live-listing fetcher.

No network: httpx.MockTransport stands in for SerpApi, so the parsing, the
cache, the rate limiter and every failure path are exercised against real
response shapes without a key or a quota.
"""

from __future__ import annotations

import time

import httpx
import pytest

from assay_watch_mcp.fetcher import (
    TTLCache,
    WatchFetcherService,
    _parse_price,
)

BRANDS = ("Rolex", "Omega", "Patek Philippe")

_SHOPPING = {
    "shopping_results": [
        {
            "position": 1,
            "title": "Rolex Submariner Date 126610LN 41mm Steel",
            "product_link": "https://shop.test/a",
            "extracted_price": 12500.0,
            "price": "$12,500.00",
            "source": "A Dealer",
            "thumbnail": "https://img.test/a.jpg",
            "second_hand_condition": "pre-owned",
            "product_id": "pid-1",
        },
        {
            "position": 2,
            "title": "Omega Speedmaster Professional",
            "product_link": "https://shop.test/b",
            "price": "£4,250",
            "source": "B Watches",
        },
    ]
}


def _service(
    handler: object, *, api_key: str = "k", min_interval_seconds: float = 0.0
) -> WatchFetcherService:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return WatchFetcherService(
        api_key=api_key,
        known_brands=BRANDS,
        client=httpx.Client(transport=transport),
        min_interval_seconds=min_interval_seconds,
    )


def _ok(payload: object) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def test_shopping_results_become_listings() -> None:
    svc = _service(_ok(_SHOPPING))
    out = svc.search("Rolex Submariner 126610LN")

    assert [x.title for x in out] == [
        "Rolex Submariner Date 126610LN 41mm Steel",
        "Omega Speedmaster Professional",
    ]
    first = out[0]
    assert first.price == 12500.0
    assert first.currency == "USD"
    assert first.merchant == "A Dealer"
    assert first.link == "https://shop.test/a"
    assert first.image_url == "https://img.test/a.jpg"
    assert first.condition == "pre-owned"
    assert first.brand == "Rolex"
    # Every row says where it came from, so a caller cannot fold these into
    # crawled data without noticing.
    assert first.source == "google_shopping"


def test_price_is_read_from_the_string_when_there_is_no_parsed_number() -> None:
    svc = _service(_ok(_SHOPPING))
    omega = svc.search("Omega Speedmaster")[1]
    assert omega.price == 4250.0
    assert omega.currency == "GBP"
    assert omega.brand == "Omega"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12500.0, 12500.0),
        ("$12,500.00", 12500.0),
        ("12.500,00 €", 12500.0),
        ("USD 12 500", 12500.0),
        ("£4,250", 4250.0),
        # A lone comma is a thousands separator unless exactly two digits
        # follow it. "12,500" is twelve and a half thousand, not twelve and a
        # half -- which for watch prices is the reading that is almost always
        # right, and the one worth being wrong about in the safer direction.
        ("12,500", 12500.0),
        ("12,50", 12.50),
        ("Price on request", None),
        ("", None),
        (None, None),
    ],
)
def test_price_parsing(raw: object, expected: float | None) -> None:
    assert _parse_price(raw)[0] == expected


def test_a_row_with_no_link_or_no_title_is_dropped() -> None:
    svc = _service(
        _ok(
            {
                "shopping_results": [
                    {"title": "No link here", "price": "$1"},
                    {"product_link": "https://x.test/1", "price": "$1"},
                    {"title": "Rolex Daytona", "product_link": "https://x.test/2"},
                    "not even a dict",
                ]
            }
        )
    )
    out = svc.search("anything")
    assert [x.title for x in out] == ["Rolex Daytona"]


def test_an_unknown_brand_is_left_empty_rather_than_guessed() -> None:
    svc = _service(
        _ok(
            {
                "shopping_results": [
                    {"title": "Seiko SKX007 Diver", "product_link": "https://x.test/1"}
                ]
            }
        )
    )
    assert svc.search("seiko")[0].brand == ""


def test_results_are_cached_so_the_same_query_is_paid_for_once() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_SHOPPING)

    svc = _service(handler)
    first = svc.search("Rolex Submariner")
    second = svc.search("rolex submariner")  # same query, different case

    assert calls["n"] == 1
    assert first == second


def test_a_different_source_is_a_different_cache_entry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"organic_results": [], "shopping_results": []})

    svc = _service(handler)
    svc.search("rolex", "google_shopping")
    svc.search("rolex", "web_search")
    assert calls["n"] == 2


def test_the_cache_expires() -> None:
    cache = TTLCache(ttl_seconds=0.05)
    cache.put("k", [])
    assert cache.get("k") == []
    time.sleep(0.08)
    assert cache.get("k") is None


def test_the_cache_evicts_the_least_recently_used() -> None:
    cache = TTLCache(maxsize=2)
    cache.put("a", [])
    cache.put("b", [])
    cache.get("a")  # 'a' is now the most recent, so 'b' should go first
    cache.put("c", [])
    assert cache.get("a") == []
    assert cache.get("b") is None
    assert cache.get("c") == []


def test_the_rate_limiter_holds_a_floor_between_calls() -> None:
    svc = _service(_ok({"shopping_results": []}), min_interval_seconds=0.15)
    started = time.monotonic()
    svc.search("one")
    svc.search("two")
    # Two distinct queries, so no cache hit to hide the wait.
    assert time.monotonic() - started >= 0.15


def test_a_transport_failure_returns_nothing_rather_than_raising() -> None:
    """A live lookup enriches crawled data that is already on screen. It must
    never be the reason a search fails."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream is down")

    assert _service(handler).search("rolex") == []


def test_an_http_error_returns_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    assert _service(handler).search("rolex") == []


def test_an_error_reported_in_a_200_body_returns_nothing() -> None:
    """SerpApi reports quota and key problems with a 200 status."""
    svc = _service(_ok({"error": "Your account has run out of searches."}))
    assert svc.search("rolex") == []


def test_a_body_that_is_not_the_expected_shape_returns_nothing() -> None:
    assert _service(_ok({"unexpected": True})).search("rolex") == []
    assert _service(_ok([1, 2, 3])).search("rolex") == []


def test_no_api_key_means_no_call_and_no_error() -> None:
    """An unconfigured deployment degrades to 'no live results', the same way
    the eBay adapter does without credentials."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the API without a key")

    svc = _service(handler, api_key="")
    assert not svc.configured
    assert svc.search("rolex") == []


def test_an_empty_query_never_reaches_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not spend a call on an empty query")

    assert _service(handler).search("   ") == []


def test_an_unsupported_source_is_rejected_before_it_is_paid_for() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("shopify_api is our own crawl, not a live lookup")

    assert _service(handler).search("rolex", "shopify_api") == []


def test_the_key_is_sent_to_the_api_and_the_engine_matches_the_source() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"organic_results": []})

    _service(handler).search("Rolex GMT", "web_search")
    assert seen["engine"] == "google"
    assert seen["q"] == "Rolex GMT"
    assert seen["api_key"] == "k"
