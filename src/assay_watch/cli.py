"""Command-line entrypoint: ``assay crawl`` and ``assay backfill-check``."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from .crawl import storage
from .crawl.runner import run_crawl
from .db.session import create_app_engine, get_session
from .logging import configure_logging, get_logger

log = get_logger("assay_watch.cli")


def _cmd_crawl(args: argparse.Namespace) -> int:
    results = run_crawl(dry_run=args.dry_run)
    for result in results:
        log.info(
            "crawl.result",
            source=result.source,
            status=result.status,
            listings_found=result.listings_found,
            seconds=round(result.duration_seconds, 2),
        )
    # Non-zero exit if any source failed, so cron/CI surfaces the problem.
    return 1 if any(r.status == "failed" for r in results) else 0


def _cmd_backfill_check(_args: argparse.Namespace) -> int:
    engine = create_app_engine()
    with get_session(engine) as session:
        rows = storage.backfill_report(session)
    if not rows:
        print("No snapshots recorded yet.")
        return 0
    print(f"{'reference':<28}{'days':>6}{'snapshots':>12}  last_seen")
    print("-" * 72)
    for row in rows:
        print(f"{row.reference:<28}{row.days:>6}{row.snapshots:>12}  {row.last_seen}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assay", description="assay-watch — luxury-watch listing snapshot spine"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="Run a crawl across all enabled sources")
    crawl.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but write nothing to the database",
    )
    crawl.set_defaults(func=_cmd_crawl)

    backfill = subparsers.add_parser(
        "backfill-check", help="Report per-reference days of history accumulated"
    )
    backfill.set_defaults(func=_cmd_backfill_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging(logging.INFO)
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
