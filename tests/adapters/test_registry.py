from __future__ import annotations

from pathlib import Path

import httpx

from assay_watch.adapters.registry import build_adapters
from assay_watch.settings import Settings

_SOURCES = """
sources:
  - type: shopify
    enabled: true
    min_interval_seconds: 0.0
    stores:
      - name: dealer
        base_url: https://dealer.test
        enabled: true
  - type: ebay
    enabled: true
  - type: shopify
    enabled: false
    stores: []
  - type: totally-unknown
    enabled: true
"""


def _settings(tmp_path: Path) -> Settings:
    sources = tmp_path / "sources.yaml"
    sources.write_text(_SOURCES)
    return Settings(sources_path=sources, references_path=tmp_path / "refs.yaml")


def test_build_adapters_respects_type_and_enabled(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"products": []}))
    adapters = build_adapters(_settings(tmp_path), transport=transport)
    # Enabled shopify + ebay, in order; disabled and unknown entries skipped.
    assert [a.name for a in adapters] == ["shopify", "ebay"]
