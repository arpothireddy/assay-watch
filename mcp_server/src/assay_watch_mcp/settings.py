"""Runtime configuration. Settings-driven, never hardcoded."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: .../mcp_server/src/assay_watch_mcp/settings.py -> parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # Read-only connection (the assay_reader role: SELECT only, nothing else).
    database_url: str = Field(alias="ASSAY_READER_DATABASE_URL")

    references_path: Path = Field(
        default=REPO_ROOT / "config" / "references.yaml", alias="ASSAY_REFERENCES_PATH"
    )

    # Host/port for the streamable-http transport (Cloud Run sets PORT).
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")


@lru_cache
def get_settings() -> Settings:
    return Settings()
