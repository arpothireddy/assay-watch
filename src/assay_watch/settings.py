"""Runtime configuration.

Settings-driven, never hardcoded: every reference, URL, selector, rate limit and
credential comes from a config file or an environment variable, never from
Python source. This module is the single place env vars are read.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: .../src/assay_watch/settings.py -> parents[2] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Process configuration, populated from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ── Postgres connection parts (URLs derived from these unless overridden) ──
    postgres_db: str = Field(default="assay", alias="POSTGRES_DB")
    postgres_app_user: str = Field(default="assay_app", alias="POSTGRES_APP_USER")
    postgres_app_password: str = Field(default="", alias="POSTGRES_APP_PASSWORD")
    postgres_migrator_user: str = Field(default="assay_migrator", alias="POSTGRES_MIGRATOR_USER")
    postgres_migrator_password: str = Field(default="", alias="POSTGRES_MIGRATOR_PASSWORD")
    postgres_host: str = Field(default="127.0.0.1", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")

    # Optional wholesale overrides (handy for CI).
    database_url_override: str | None = Field(default=None, alias="ASSAY_DATABASE_URL")
    migrator_database_url_override: str | None = Field(
        default=None, alias="ASSAY_MIGRATOR_DATABASE_URL"
    )

    # ── Crawler identity ──────────────────────────────────────────────────────
    contact_url: str = Field(
        default="https://example.com/assay-watch-crawler", alias="ASSAY_CONTACT_URL"
    )
    user_agent: str = Field(
        default="assay-watch/0.0 (+https://example.com/assay-watch-crawler)",
        alias="ASSAY_USER_AGENT",
    )

    # ── Config file locations ─────────────────────────────────────────────────
    references_path: Path = Field(
        default=REPO_ROOT / "config" / "references.yaml", alias="ASSAY_REFERENCES_PATH"
    )
    sources_path: Path = Field(
        default=REPO_ROOT / "config" / "sources.yaml", alias="ASSAY_SOURCES_PATH"
    )

    # ── Observability ─────────────────────────────────────────────────────────
    # Where to write the Prometheus textfile (.prom). None disables metric output
    # (e.g. in tests). A daily batch job is a poor fit for scrape endpoints, so we
    # write a file the node_exporter textfile collector reads.
    metrics_dir: Path | None = Field(default=None, alias="ASSAY_METRICS_DIR")

    # ── eBay (unconfigured until developer-program access is confirmed) ───────
    ebay_client_id: str = Field(default="", alias="ASSAY_EBAY_CLIENT_ID")
    ebay_client_secret: str = Field(default="", alias="ASSAY_EBAY_CLIENT_SECRET")

    def _url(self, user: str, password: str) -> str:
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url(self) -> str:
        """Least-privilege application connection (INSERT/SELECT + crawl_runs UPDATE)."""
        return self.database_url_override or self._url(
            self.postgres_app_user, self.postgres_app_password
        )

    @property
    def migrator_database_url(self) -> str:
        """Migrator connection with DDL rights, used only by Alembic."""
        return self.migrator_database_url_override or self._url(
            self.postgres_migrator_user, self.postgres_migrator_password
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
