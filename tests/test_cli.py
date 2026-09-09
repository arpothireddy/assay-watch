from __future__ import annotations

import pytest

from assay_watch import cli
from assay_watch.crawl.runner import SourceResult


def test_parser_crawl_dry_run() -> None:
    args = cli.build_parser().parse_args(["crawl", "--dry-run"])
    assert args.dry_run is True


def test_cmd_crawl_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    args = cli.build_parser().parse_args(["crawl"])

    monkeypatch.setattr(
        cli, "run_crawl", lambda dry_run: [SourceResult("shopify", "success", 1, 0.1)]
    )
    assert cli._cmd_crawl(args) == 0

    monkeypatch.setattr(
        cli, "run_crawl", lambda dry_run: [SourceResult("shopify", "failed", 0, 0.1)]
    )
    assert cli._cmd_crawl(args) == 1
