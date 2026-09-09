"""eBay adapter — registered, but a stub until API access is confirmed.

eBay is the highest-value source (the only large-volume source of real *sold*
prices), but access is gated by the developer program and the Marketplace
Insights tier is an open question. Rather than ship unverifiable API code, this
adapter proves the interface generalises to a credentialed, JSON source and
reports itself ``unconfigured`` until credentials exist — at which point the
crawl runner skips it (an expected state, not a failure).

When developer-program access is granted, implement :meth:`fetch` against the
Browse API (live listings) and Marketplace Insights (sold data). Until then it
is never called, because the runner gates on ``health_check()``.
"""

from __future__ import annotations

from .base import HealthCheck, HealthStatus, RawListing, Reference, SourceAdapter


class EbayAdapter(SourceAdapter):
    name = "ebay"

    def __init__(self, *, client_id: str, client_secret: str, marketplace: str = "EBAY_US") -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._marketplace = marketplace

    def health_check(self) -> HealthCheck:
        if not (self._client_id and self._client_secret):
            return HealthCheck(
                status=HealthStatus.UNCONFIGURED,
                detail="eBay client credentials not set; adapter skipped",
            )
        return HealthCheck(status=HealthStatus.OK)

    def fetch(self, reference: Reference) -> list[RawListing]:
        # Unreachable while unconfigured (the runner skips it). If credentials are
        # present, that means access was granted and the real Browse / Marketplace
        # Insights integration is the next thing to build here.
        raise NotImplementedError(
            "eBay Browse/Marketplace Insights integration is not implemented yet; "
            "it lands once developer-program access is confirmed."
        )
