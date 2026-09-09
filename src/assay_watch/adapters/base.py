"""The source adapter interface and its data types.

An adapter turns "give me listings for this reference" into a list of
``RawListing`` records. Adapters are deliberately dumb: they fetch and shape the
source's own data, they do not parse references, condition, or completeness
(that is Phase 2). Every adapter fails independently — the crawl runner isolates
one adapter's failure from the rest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Reference(BaseModel):
    """A canonical watch reference from ``config/references.yaml``."""

    ref: str
    brand: str
    model_name: str
    search_aliases: list[str] = Field(default_factory=list)
    enabled: bool = True

    def all_terms(self) -> list[str]:
        """The reference plus its aliases — the strings a source might use."""
        return [self.ref, *self.search_aliases]


class RawListing(BaseModel):
    """One listing as an adapter observed it, before any normalization.

    ``raw_payload`` holds the source's own response object verbatim; the other
    fields are conveniences promoted out of it.
    """

    source_listing_id: str
    url: str
    raw_title: str
    price_amount: Decimal | None = None
    price_currency: str | None = None
    seller_name: str | None = None
    seller_country: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class HealthStatus(StrEnum):
    OK = "ok"
    # The adapter is registered but intentionally not runnable yet (e.g. missing
    # credentials). The crawl skips it — this is not a failure.
    UNCONFIGURED = "unconfigured"
    ERROR = "error"


class HealthCheck(BaseModel):
    status: HealthStatus
    detail: str = ""


class SourceAdapter(ABC):
    """Base class for all sources. Synchronous by design: at Phase 0 volumes
    (polite, rate-limited) sync is simpler and more reliable than async."""

    #: Stable adapter name; also the value written to ``listing_snapshots.source``.
    name: str

    @abstractmethod
    def fetch(self, reference: Reference) -> list[RawListing]:
        """Return current listings the source has for ``reference``."""

    @abstractmethod
    def health_check(self) -> HealthCheck:
        """Report whether the adapter is ready to run."""
