"""Live listing retrieval, as an MCP tool the orchestrator can choose to call.

This sits beside the crawl rather than inside it. The crawl is a daily batch
over dealer storefronts we vetted one by one; this is a synchronous lookup
against a licensed results API, for the case where a buyer asks about
something our dealers do not stock. Those are different guarantees, so they
stay different code paths and -- everywhere it surfaces -- different labels.

Why SerpApi rather than fetching Google directly: Google's robots.txt
disallows /search and its terms prohibit automated access, which
``docs/SOURCES.md`` already rules out for every source (it is why Chrono24 is
flagged do-not-scrape). A licensed results API carries that compliance
posture contractually, which is the whole reason to pay for one.

Three things keep this from becoming a liability:

- **Rate limiting.** A hard floor between outbound calls, so a burst of
  searches cannot spend the month's quota in a minute.
- **Caching.** Watch prices move over days, not seconds. A short TTL turns
  repeated searches for the same popular reference into one paid call.
- **Fallback.** Every failure path returns an empty list and logs. A live
  lookup is an enrichment on top of crawled data that is already on screen;
  it must never be the reason a search fails.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ListingSource = Literal["google_shopping", "web_search", "shopify_api"]

_SERPAPI_ENDPOINT = "https://serpapi.com/search"

# Engines we know how to parse. Anything else is rejected at the boundary
# rather than passed through to the API and charged for.
_ENGINE_FOR_SOURCE: dict[str, str] = {
    "google_shopping": "google_shopping",
    "web_search": "google",
}

# A price like "$12,500.00", "USD 12,500" or "12.500,00 €". Only used when the
# API gives no parsed number of its own.
_PRICE_RE = re.compile(r"(\d[\d.,\s]*)")
_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "CHF": "CHF"}


class WatchListing(BaseModel):
    """One listing, whatever it came from.

    ``price`` is a float here because a results API hands back an already
    parsed number and there is nothing more faithful to preserve. That is a
    deliberate difference from the crawl's own ``Listing``, whose prices are
    Decimal strings read off a dealer's storefront and are the ones a
    fair-price figure may be computed from. Nothing in this module feeds a
    median: ``source`` is on every row precisely so a caller cannot merge
    these with crawled data by accident.
    """

    id: str
    title: str
    brand: str = ""
    price: float | None = None
    currency: str = "USD"
    merchant: str = ""
    link: str
    image_url: str | None = Field(default=None, alias="imageUrl")
    condition: str | None = None
    source: ListingSource

    model_config = {"populate_by_name": True}


class TTLCache:
    """Small LRU with a time-to-live, kept in process.

    Redis would be the reach for a multi-instance deployment, and this
    deliberately is not that: the service runs on Cloud Run where instances
    come and go, a cold instance simply pays for one lookup, and a cache miss
    costs a few hundred milliseconds rather than correctness. Adding Redis
    would mean provisioning, a connection to manage and a second thing that
    can be down, to save a fraction of an API quota that the rate limiter
    already bounds. Worth revisiting when there is sustained traffic to
    measure; not before.
    """

    def __init__(self, *, maxsize: int = 256, ttl_seconds: float = 900.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._data: OrderedDict[str, tuple[float, list[WatchListing]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[WatchListing] | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            stored_at, value = hit
            if time.monotonic() - stored_at > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: str, value: list[WatchListing]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class _RateLimiter:
    """A hard floor between outbound calls, shared across threads.

    Blocking rather than rejecting: a caller that waits 300ms gets its
    answer, where one that gets refused has to be handled everywhere up the
    stack. The floor is small enough that a single request never notices and
    a runaway loop cannot outrun it.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


def _parse_price(raw: Any) -> tuple[float | None, str]:
    """Best effort at (amount, currency) from whatever the API returned.

    Returns ``(None, "USD")`` rather than guessing when the string cannot be
    read: a listing with no price is still useful to show, and a wrong number
    is worse than a blank.
    """
    if isinstance(raw, int | float):
        return float(raw), "USD"
    if not isinstance(raw, str) or not raw.strip():
        return None, "USD"

    currency = "USD"
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in raw:
            currency = code
            break

    match = _PRICE_RE.search(raw)
    if match is None:
        return None, currency
    digits = match.group(1).strip().replace(" ", "")
    # "12,500.00" is thousands-then-decimal; "12.500,00" is the reverse.
    if "," in digits and "." in digits:
        if digits.rindex(",") > digits.rindex("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif "," in digits:
        # A lone comma is a decimal separator only with exactly two digits
        # after it; otherwise it is a thousands separator.
        digits = digits.replace(",", "." if len(digits.split(",")[-1]) == 2 else "")
    try:
        return float(digits), currency
    except ValueError:
        return None, currency


def _brand_from(title: str, known_brands: tuple[str, ...]) -> str:
    """The brand, only when the title actually names one we track.

    Never guessed from the rest of the title: an empty brand is a filter that
    matches nothing, while a wrong one files the watch under someone else's
    name.
    """
    lowered = title.lower()
    for brand in known_brands:
        if brand.lower() in lowered:
            return brand
    return ""


class WatchFetcherService:
    """Fetch live listings for a free-text watch query.

    Reports itself unconfigured when no API key is set, exactly as the eBay
    adapter does, so a deployment without a SerpApi subscription degrades to
    "no live results" rather than erroring on every search.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        known_brands: tuple[str, ...] = (),
        client: httpx.Client | None = None,
        cache: TTLCache | None = None,
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 12.0,
        max_results: int = 20,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._known_brands = known_brands
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._cache = cache if cache is not None else TTLCache()
        self._limiter = _RateLimiter(min_interval_seconds)
        self._max_results = max_results

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, source: ListingSource = "google_shopping") -> list[WatchListing]:
        """Listings for ``query``. Never raises -- an empty list means either
        nothing was found or the lookup failed, and the log says which."""
        query = (query or "").strip()
        if not query:
            return []
        engine = _ENGINE_FOR_SOURCE.get(source)
        if engine is None:
            logger.warning("live search asked for unsupported source %r", source)
            return []
        if not self.configured:
            logger.info("live search skipped for %r: no SerpApi key configured", query)
            return []

        cache_key = f"{source}:{query.lower()}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("live search cache hit for %r", cache_key)
            return cached

        self._limiter.acquire()
        try:
            response = self._client.get(
                _SERPAPI_ENDPOINT,
                params={
                    "engine": engine,
                    "q": query,
                    "api_key": self._api_key,
                    "num": self._max_results,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            # Deliberately broad: a timeout, a 429, a 500, a body that is not
            # JSON -- from the caller's side these are the same event, and
            # none of them is worth failing a search over.
            logger.exception("live search failed for %r via %s", query, source)
            return []

        if isinstance(payload, dict) and payload.get("error"):
            # SerpApi reports quota and key problems in a 200 body.
            logger.warning("live search rejected for %r: %s", query, payload["error"])
            return []

        listings = self._parse(payload, source)
        self._cache.put(cache_key, listings)
        logger.info(
            "live search for %r via %s returned %d listing(s)", query, source, len(listings)
        )
        return listings

    def _parse(self, payload: Any, source: ListingSource) -> list[WatchListing]:
        if not isinstance(payload, dict):
            return []
        key = "shopping_results" if source == "google_shopping" else "organic_results"
        rows = payload.get(key)
        if not isinstance(rows, list):
            return []

        listings: list[WatchListing] = []
        for index, row in enumerate(rows[: self._max_results]):
            if not isinstance(row, dict):
                continue
            link = row.get("product_link") or row.get("link")
            title = row.get("title")
            if not link or not title:
                # Without a destination the row is unusable, and without a
                # title there is nothing to show or match a brand against.
                continue
            amount, currency = _parse_price(row.get("extracted_price") or row.get("price"))
            listings.append(
                WatchListing(
                    id=str(row.get("product_id") or row.get("position") or index),
                    title=str(title),
                    brand=_brand_from(str(title), self._known_brands),
                    price=amount,
                    currency=str(row.get("currency") or currency),
                    merchant=str(row.get("source") or row.get("displayed_link") or ""),
                    link=str(link),
                    image_url=row.get("thumbnail"),
                    condition=row.get("second_hand_condition") or row.get("condition"),
                    source=source,
                )
            )
        return listings
