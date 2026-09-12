from __future__ import annotations

from decimal import Decimal

import httpx

from assay_watch.adapters.base import HealthStatus, Reference
from assay_watch.adapters.http import PoliteClient
from assay_watch.adapters.shopify import ShopifyAdapter, ShopifyStore, _compact
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


def _matches(product: dict[str, object], ref: str, *aliases: str) -> bool:
    adapter = ShopifyAdapter.__new__(ShopifyAdapter)
    reference = Reference(ref=ref, brand="b", model_name="m", search_aliases=list(aliases) or [ref])
    return adapter._matches(product, [_compact(t) for t in reference.all_terms()])


def _product(title: str, **kw: object) -> dict[str, object]:
    return {
        "title": title,
        "vendor": kw.get("vendor", ""),
        "product_type": "Watches",
        "handle": title.lower().replace(" ", "-"),
        "tags": kw.get("tags", []),
        "variants": [{"sku": kw.get("sku", ""), "title": "Default Title"}],
    }


# Every case below is a real row this pulled into production: the reference
# on the right was recorded as matching the listing on the left.
def test_price_tag_and_material_do_not_combine_into_a_reference() -> None:
    """The bug that made this matter. A $15,500 Datejust tagged "15500" and
    "Stainless Steel" fused to "...15500stainlesssteel..." and registered as
    an Audemars Piguet Royal Oak 15500ST -- so the site reported a Datejust
    as the cheapest Royal Oak on the market."""
    datejust = _product("Rolex Datejust 41 126333", tags=["15500", "Stainless Steel"])
    assert not _matches(datejust, "15500ST")

    kermit = _product("Rolex Submariner Kermit 16610LV", tags=["15400", "Steel"])
    assert not _matches(kermit, "15400ST")

    iwc = _product("IWC Portuguese Perpetual Calendar 42", tags=["15500", "Steel"])
    assert not _matches(iwc, "15500ST")


def test_reference_may_not_start_or_end_mid_token() -> None:
    assert not _matches(_product("Malo Black Tungsten Wedding Ring", sku="57111A2345"), "5711/1A")
    assert not _matches(_product("Tag Heuer 2000 Exclusive WN1353"), "5711/1A")


def test_a_neighbouring_generation_is_not_the_tracked_reference() -> None:
    """116710BLNR is the previous Batman and 116520 the previous Daytona --
    different watches at different prices, so folding them in would skew the
    fair price for the references we do track."""
    assert not _matches(_product("2015 Rolex GMT II (Ref. 116710 BLNR) Batman"), "126710BLNR")
    assert not _matches(_product("2013 Rolex Daytona 116520 White Dial"), "116500LN")


def test_references_still_match_however_dealers_write_them() -> None:
    assert _matches(_product("2016 Rolex Ceramic Daytona 116500LN Black Dial"), "116500LN")
    assert _matches(_product("Rolex Oyster Perpetual 41 124300"), "124300")
    # Separators inside the reference itself must not break the match.
    assert _matches(
        _product("OMEGA Speedmaster Moonwatch 310.30.42.50.01.002"), "310.30.42.50.01.002"
    )
    # A reference split across tokens in the title.
    assert _matches(_product("Rolex Submariner Date 126610 LN"), "126610LN", "126610LN")
    # A longer factory reference string that begins with ours.
    assert _matches(_product("AP Royal Oak 15500ST.OO.1220ST.01"), "15500ST")
    # A reference living in the SKU rather than the title.
    assert _matches(_product("Rolex Submariner", sku="126610LN"), "126610LN")


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


def test_one_store_failure_does_not_block_others() -> None:
    """A store whose catalog fetch raises (robots disallow, network error, ...)
    must not sink every other store's results for the reference -- and, since a
    failed fetch is never cached, must not keep re-raising on every subsequent
    reference either."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad.test":
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            return httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/products.json":
            page = int(request.url.params.get("page", "1"))
            products = _PRODUCTS if page == 1 else []
            return httpx.Response(200, json={"products": products})
        return httpx.Response(404)

    client = PoliteClient(
        user_agent="assay-test", min_interval_seconds=0.0, transport=make_transport(handler)
    )
    bad_store = ShopifyStore(name="bad", base_url="https://bad.test", currency="USD")
    good_store = ShopifyStore(name="good", base_url="https://good.test", currency="USD")
    adapter = ShopifyAdapter(stores=[bad_store, good_store], client=client)
    ref = Reference(ref="126610LN", brand="Rolex", model_name="Submariner", search_aliases=[])

    listings = adapter.fetch(ref)

    assert len(listings) == 1
    assert listings[0].source_listing_id == "good:111"


def test_an_unset_store_currency_is_carried_through_as_none() -> None:
    """Currency comes from config, never from the product payload, so a store
    whose presentment currency is unconfirmed must stay null all the way
    through. The pricing queries only ever total ``price_currency = 'USD'``,
    so a null is counted as non-USD and excluded -- whereas guessing 'USD' on
    a GBP or CAD store would price those listings as dollars and corrupt
    every median they landed in. Visibly missing beats quietly wrong."""
    counter = {"pages": 0}
    client = PoliteClient(
        user_agent="assay-test", min_interval_seconds=0.0, transport=_handler(counter)
    )
    adapter = ShopifyAdapter(
        client=client,
        # No currency= -- exactly how bremont and halios-watches are declared.
        stores=[ShopifyStore(name="unconfirmed", base_url="https://dealer.test")],
    )

    listings = adapter.fetch(Reference(ref="126610LN", brand="Rolex", model_name="Submariner Date"))
    assert len(listings) == 1
    assert listings[0].price_currency is None
    assert listings[0].price_amount == Decimal("13500.00")
