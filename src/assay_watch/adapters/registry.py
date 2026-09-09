"""Config-driven adapter registry.

Adapters are declared in ``config/sources.yaml`` and instantiated here — never
wired up by scattered imports. Adding a source is a config change plus one
builder branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml

from ..logging import get_logger
from ..settings import Settings
from .base import SourceAdapter
from .ebay import EbayAdapter
from .http import PoliteClient
from .shopify import ShopifyAdapter, ShopifyStore

log = get_logger(__name__)

_DEFAULT_MIN_INTERVAL = 3.0


def load_sources_config(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


def build_adapters(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> list[SourceAdapter]:
    """Instantiate every enabled adapter declared in the sources config.

    ``transport`` is injected in tests to serve canned HTTP responses.
    """
    config = load_sources_config(settings.sources_path)
    adapters: list[SourceAdapter] = []

    for entry in config.get("sources", []):
        if not entry.get("enabled", True):
            continue
        source_type = entry.get("type")

        if source_type == "shopify":
            client = PoliteClient(
                user_agent=settings.user_agent,
                min_interval_seconds=float(
                    entry.get("min_interval_seconds", _DEFAULT_MIN_INTERVAL)
                ),
                transport=transport,
            )
            stores = [ShopifyStore(**store) for store in entry.get("stores", [])]
            adapters.append(ShopifyAdapter(stores=stores, client=client))

        elif source_type == "ebay":
            adapters.append(
                EbayAdapter(
                    client_id=settings.ebay_client_id,
                    client_secret=settings.ebay_client_secret,
                    marketplace=str(entry.get("marketplace", "EBAY_US")),
                )
            )

        else:
            log.warning("registry.unknown_source_type", type=source_type)

    return adapters
