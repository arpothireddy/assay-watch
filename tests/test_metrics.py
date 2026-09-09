from __future__ import annotations

from pathlib import Path

from assay_watch.metrics import CrawlMetrics


def test_write_creates_prom_file(tmp_path: Path) -> None:
    metrics = CrawlMetrics()
    metrics.record("shopify", "success", 3, 1.25)
    metrics.record("ebay", "skipped", 0, 0.0)
    metrics.write(tmp_path)

    prom = tmp_path / "assay_watch.prom"
    assert prom.exists()
    text = prom.read_text()
    assert "assay_crawl_listings_found" in text
    assert 'source="shopify"' in text
    assert 'status="success"' in text


def test_write_none_is_noop() -> None:
    CrawlMetrics().write(None)  # must not raise
