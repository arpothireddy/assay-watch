from __future__ import annotations

from decimal import Decimal

import httpx

from assay_watch.adapters.base import HealthStatus, Reference
from assay_watch.adapters.http import PoliteClient
from assay_watch.adapters.shopify import ShopifyAdapter, ShopifyStore
from tests.conftest import make_transport

_PRODUCTS = [
    {
        "id": 111,
        "title": "Rolex Submariner Date 126610LN Full Set 2023",
        "handle": "rolex-submariner-126610ln",
        "vendor": "Rolex",
        "product_type": "Watch",
        "tags": ["Rolex", "Submariner"],
        "variants": [{"sku": "126610LN", "price": "13500.00", "title": "Default"}],
    },
    {
        "id": 222,
        "title": "Omega Speedmaster Moonwatch",
        "handle": "omega-speedmaster",
        "variants": [{"sku": "OMEGA-1", "price": "6200.00"}],
    },
]


def _handler(counter: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/products.json":
            page = int(request.url.params.get("page", "1"))
            counter["pages"] += 1
            products = _PRODUCTS if page == 1 else []
            return httpx.Response(200, json={"products": products})
        return httpx.Response(404)

    return make_transport(handler)


def _adapter(counter: dict[str, int]) -> ShopifyAdapter:
    client = PoliteClient(
        user_agent="assay-test", min_interval_seconds=0.0, transport=_handler(counter)
    )
    store = ShopifyStore(name="dealer", base_url="https://dealer.test", currency="USD")
    return ShopifyAdapter(stores=[store], client=client)


def test_matches_and_maps_listing() -> None:
    adapter = _adapter({"pages": 0})
    ref = Reference(ref="126610LN", brand="Rolex", model_name="Submariner", search_aliases=[])
    listings = adapter.fetch(ref)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.source_listing_id == "dealer:111"
    assert listing.url == "https://dealer.test/products/rolex-submariner-126610ln"
    assert listing.price_amount == Decimal("13500.00")
    assert listing.price_currency == "USD"
    assert listing.raw_payload["id"] == 111  # verbatim source object retained


def test_no_match_returns_empty() -> None:
    adapter = _adapter({"pages": 0})
    ref = Reference(ref="5711/1A", brand="Patek", model_name="Nautilus", search_aliases=[])
    assert adapter.fetch(ref) == []


def test_catalog_is_fetched_once_across_references() -> None:
    counter = {"pages": 0}
    adapter = _adapter(counter)
    ref_a = Reference(ref="126610LN", brand="Rolex", model_name="Sub")
    ref_b = Reference(ref="OMEGA-1", brand="Omega", model_name="Speedmaster")
    adapter.fetch(ref_a)
    pages_after_first = counter["pages"]
    adapter.fetch(ref_b)
    assert counter["pages"] == pages_after_first  # cached, no refetch


def test_health_unconfigured_without_stores() -> None:
    client = PoliteClient(user_agent="assay-test", min_interval_seconds=0.0)
    adapter = ShopifyAdapter(stores=[], client=client)
    assert adapter.health_check().status is HealthStatus.UNCONFIGURED
