"""Shopify storefront ``/products.json`` adapter.

Many independent watch dealers run Shopify, which serves structured product JSON
at a public ``/products.json`` endpoint — no HTML parsing, no JS, no anti-bot
fight. The endpoint has no server-side search, so we fetch a store's catalogue
once per run (cached) and match products to a reference client-side.

The matching here is deliberately crude — a normalized substring check against
the reference and its aliases. It only *associates* a listing with the reference
being queried; it does not extract or normalize anything (that is Phase 2). Over-
inclusion is acceptable and is recorded via ``search_reference`` on each row.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from ..logging import get_logger
from .base import HealthCheck, HealthStatus, RawListing, Reference, SourceAdapter
from .http import PoliteClient

log = get_logger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase and strip everything but letters and digits."""
    return _NON_ALNUM.sub("", text.lower())


class ShopifyStore(BaseModel):
    name: str
    base_url: str
    currency: str | None = None
    enabled: bool = True


class ShopifyAdapter(SourceAdapter):
    name = "shopify"

    def __init__(
        self,
        *,
        stores: list[ShopifyStore],
        client: PoliteClient,
        page_limit: int = 250,
        max_pages: int = 20,
    ) -> None:
        self._stores = [s for s in stores if s.enabled]
        self._client = client
        self._page_limit = page_limit
        self._max_pages = max_pages
        # Catalogue cached per store for the life of this adapter (one crawl run),
        # so each store is fetched once regardless of how many references we query.
        self._catalog: dict[str, list[dict[str, Any]]] = {}

    def health_check(self) -> HealthCheck:
        if not self._stores:
            return HealthCheck(status=HealthStatus.UNCONFIGURED, detail="no enabled Shopify stores")
        return HealthCheck(status=HealthStatus.OK)

    def _load_catalog(self, store: ShopifyStore) -> list[dict[str, Any]]:
        cached = self._catalog.get(store.base_url)
        if cached is not None:
            return cached

        products: list[dict[str, Any]] = []
        for page in range(1, self._max_pages + 1):
            resp = self._client.get(
                f"{store.base_url.rstrip('/')}/products.json",
                params={"limit": self._page_limit, "page": page},
            )
            if resp.status_code != 200:
                log.warning(
                    "shopify.page_failed",
                    store=store.name,
                    page=page,
                    status=resp.status_code,
                )
                break
            batch = resp.json().get("products", [])
            if not batch:
                break
            products.extend(batch)
            if len(batch) < self._page_limit:
                break

        self._catalog[store.base_url] = products
        return products

    @staticmethod
    def _haystack(product: dict[str, Any]) -> str:
        parts: list[str] = [
            str(product.get("title", "")),
            str(product.get("vendor", "")),
            str(product.get("product_type", "")),
            str(product.get("handle", "")),
        ]
        tags = product.get("tags")
        if isinstance(tags, list):
            parts.extend(str(t) for t in tags)
        elif isinstance(tags, str):
            parts.append(tags)
        for variant in product.get("variants", []) or []:
            if isinstance(variant, dict):
                parts.append(str(variant.get("sku", "")))
                parts.append(str(variant.get("title", "")))
        return _normalize(" ".join(parts))

    def _matches(self, product: dict[str, Any], normalized_terms: list[str]) -> bool:
        haystack = self._haystack(product)
        return any(term and term in haystack for term in normalized_terms)

    def _to_listing(self, store: ShopifyStore, product: dict[str, Any]) -> RawListing:
        variants = product.get("variants") or []
        first = variants[0] if variants and isinstance(variants[0], dict) else {}
        price_amount: Decimal | None = None
        raw_price = first.get("price")
        if raw_price not in (None, ""):
            try:
                price_amount = Decimal(str(raw_price))
            except (InvalidOperation, ValueError):
                log.warning("shopify.bad_price", store=store.name, price=raw_price)
        handle = str(product.get("handle", ""))
        return RawListing(
            source_listing_id=f"{store.name}:{product.get('id')}",
            url=f"{store.base_url.rstrip('/')}/products/{handle}",
            raw_title=str(product.get("title", "")),
            price_amount=price_amount,
            price_currency=store.currency,
            seller_name=store.name,
            seller_country=None,
            raw_payload=product,
        )

    def fetch(self, reference: Reference) -> list[RawListing]:
        terms = [_normalize(t) for t in reference.all_terms()]
        listings: list[RawListing] = []
        for store in self._stores:
            for product in self._load_catalog(store):
                if self._matches(product, terms):
                    listings.append(self._to_listing(store, product))
        return listings
