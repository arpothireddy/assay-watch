"""initial schema: crawl_runs + listing_snapshots

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00
"""
from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from assay_watch.settings import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Postgres identifiers we interpolate must be plain identifiers — never trust an
# arbitrary string in DDL.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def upgrade() -> None:
    op.create_table(
        "crawl_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="failed"),
        sa.Column("listings_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )
    op.create_table(
        "listing_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_listing_id", sa.Text(), nullable=False),
        sa.Column("search_reference", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("raw_title", sa.Text(), nullable=False),
        sa.Column("price_amount", sa.Numeric(), nullable=True),
        sa.Column("price_currency", sa.Text(), nullable=True),
        sa.Column("seller_name", sa.Text(), nullable=True),
        sa.Column("seller_country", sa.Text(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "crawl_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crawl_runs.id"),
            nullable=False,
        ),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source", "source_listing_id", "crawl_run_id", name="uq_snapshot_per_run"
        ),
    )
    op.create_index(
        "ix_snapshot_source_listing_seen",
        "listing_snapshots",
        ["source", "source_listing_id", sa.text("seen_at DESC")],
    )
    op.create_index("ix_snapshot_content_hash", "listing_snapshots", ["content_hash"])

    _grant_app_privileges()


def _grant_app_privileges() -> None:
    """Grant the least-privilege application role exactly what it needs:
    INSERT/SELECT on both tables, UPDATE on crawl_runs only (so listing_snapshots
    is append-only at the database level), and sequence usage for the bigserial
    key. No-op if the role does not exist (e.g. a test/CI database)."""
    app_user = get_settings().postgres_app_user
    if not _IDENT.match(app_user):
        return
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_user}') THEN
            GRANT SELECT, INSERT ON TABLE listing_snapshots TO {app_user};
            GRANT SELECT, INSERT, UPDATE ON TABLE crawl_runs TO {app_user};
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_user};
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_snapshot_content_hash", table_name="listing_snapshots")
    op.drop_index("ix_snapshot_source_listing_seen", table_name="listing_snapshots")
    op.drop_table("listing_snapshots")
    op.drop_table("crawl_runs")
