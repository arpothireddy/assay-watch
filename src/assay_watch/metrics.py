"""Prometheus metrics for the crawl.

A once-daily crawl is a short-lived process, so an HTTP scrape endpoint is the
wrong shape — Prometheus would mostly scrape a dead target. Instead we write a
``.prom`` file that the node_exporter textfile collector picks up. Still the
``prometheus-client`` library, delivered in the way that fits a batch job.
"""

from __future__ import annotations

import time
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, write_to_textfile

# Terminal states a per-source run can end in. "skipped" covers an adapter that
# reported itself unconfigured (e.g. eBay without credentials) — an expected,
# non-failure state.
_STATUSES = ("success", "partial", "failed", "skipped")

_METRICS_FILENAME = "assay_watch.prom"


class CrawlMetrics:
    """Collects per-source crawl metrics and writes them to a textfile."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.run_status = Gauge(
            "assay_crawl_run_status",
            "1 for the status the source's last run ended in, else 0.",
            ["source", "status"],
            registry=self.registry,
        )
        self.listings_found = Gauge(
            "assay_crawl_listings_found",
            "Listings found in the source's last run.",
            ["source"],
            registry=self.registry,
        )
        self.duration_seconds = Gauge(
            "assay_crawl_run_duration_seconds",
            "Wall-clock duration of the source's last run.",
            ["source"],
            registry=self.registry,
        )
        self.last_run_timestamp = Gauge(
            "assay_crawl_last_run_timestamp_seconds",
            "Unix timestamp when the source's last run finished.",
            ["source"],
            registry=self.registry,
        )

    def record(
        self, source: str, status: str, listings_found: int, duration_seconds: float
    ) -> None:
        """Record the outcome of one source's run."""
        for candidate in _STATUSES:
            self.run_status.labels(source=source, status=candidate).set(
                1.0 if candidate == status else 0.0
            )
        self.listings_found.labels(source=source).set(listings_found)
        self.duration_seconds.labels(source=source).set(duration_seconds)
        self.last_run_timestamp.labels(source=source).set(time.time())

    def write(self, metrics_dir: Path | None) -> None:
        """Write the textfile if a metrics directory is configured; else no-op."""
        if metrics_dir is None:
            return
        metrics_dir.mkdir(parents=True, exist_ok=True)
        write_to_textfile(str(metrics_dir / _METRICS_FILENAME), self.registry)
