"""Shopify storefront ``/products.json`` adapter.

Many independent watch dealers run Shopify, which serves structured product JSON
at a public ``/products.json`` endpoint — no HTML parsing, no JS, no anti-bot
fight. The endpoint has no server-side search, so we fetch a store's catalogue
once per run (cached) and match products to a reference client-side.

Matching associates a listing with the reference being queried; it does not
extract or normalize anything (that is Phase 2). Some over-inclusion is
acceptable and is recorded via ``search_reference`` on each row -- but only
over-inclusion a human would call arguable, not the kind below.

A reference has to line up with whole words. An earlier version lowercased
each field, deleted every non-alphanumeric character, and substring-searched
the concatenation, which went wrong two ways at once. Deleting the
separators let a term straddle a field boundary: a $15,500 Rolex Datejust
tagged ``15500`` / ``Stainless Steel`` fused to ``...15500stainlesssteel...``
and so "contained" ``15500ST``, an Audemars Piguet Royal Oak. Unbounded
substring search then let a term land mid-token as well. Between them,
whichever watch happened to cost $15,500 was filed as a Royal Oak, and the
site quoted it as the cheapest one on the market.
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

_ALNUM_RUN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric runs. Punctuation separates rather than
    vanishing, so ``5711/1A`` is ``["5711", "1a"]`` -- two tokens that stay
    two tokens."""
    return _ALNUM_RUN.findall(text.lower())


def _compact(text: str) -> str:
    return "".join(_tokens(text))


def _spans_whole_tokens(tokens: list[str], term: str) -> bool:
    """True when ``term`` is spelled exactly by consecutive whole tokens.

    References are written inconsistently across dealers -- ``126610LN``,
    ``126610 LN``, ``310.30.42.50.01.002`` -- so a term is allowed to run
    across several tokens. What it may not do is start or stop mid-token:
    that is the difference between matching ``15500ST`` and matching the
    ``15500`` in a price tag followed by a word starting with "st".
    """
    for start in range(len(tokens)):
        buf = ""
        for token in tokens[start:]:
            buf += token
            if buf == term:
                return True
            if not term.startswith(buf):
                break
    return False


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
    def _match_fields(product: dict[str, Any]) -> list[list[str]]:
        """Each searchable field tokenized on its own.

        Kept as separate lists rather than one merged stream: a reference is
        only a match if it appears within a single field. Two adjacent fields
        must never combine to spell one -- that is exactly how a price tag
        and a material name together spelled a reference number.
        """
        fields: list[str] = [
            str(product.get("title", "")),
            str(product.get("vendor", "")),
            str(product.get("product_type", "")),
            str(product.get("handle", "")),
        ]
        tags = product.get("tags")
        if isinstance(tags, list):
            fields.extend(str(t) for t in tags)
        elif isinstance(tags, str):
            fields.append(tags)
        for variant in product.get("variants", []) or []:
            if isinstance(variant, dict):
                fields.append(str(variant.get("sku", "")))
                fields.append(str(variant.get("title", "")))
        return [_tokens(f) for f in fields if f]

    def _matches(self, product: dict[str, Any], compact_terms: list[str]) -> bool:
        fields = self._match_fields(product)
        return any(
            term and any(_spans_whole_tokens(tokens, term) for tokens in fields)
            for term in compact_terms
        )

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
        terms = [_compact(t) for t in reference.all_terms()]
        listings: list[RawListing] = []
        for store in self._stores:
            try:
                catalog = self._load_catalog(store)
            except Exception as exc:  # noqa: BLE001 - one bad store must not
                # sink every other store's results for this reference (and,
                # since a failed fetch is never cached, it would otherwise
                # re-raise on every subsequent reference too).
                log.warning(
                    "shopify.store_failed", store=store.name, error=f"{type(exc).__name__}: {exc}"
                )
                continue
            for product in catalog:
                if self._matches(product, terms):
                    listings.append(self._to_listing(store, product))
        return listings
