"""Runtime configuration. Settings-driven, never hardcoded."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    gemini_api_key: str = Field(alias="GEMINI_API_KEY")
    mcp_server_url: str = Field(alias="ASSAY_MCP_SERVER_URL")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", alias="ASSAY_GEMINI_MODEL")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")


@lru_cache
def get_settings() -> Settings:
    return Settings()
