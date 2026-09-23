"""Runtime configuration, loaded from environment / `.env`. No hardcoded secrets."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Legacy (business data source, READ ONLY) ---------------------------
    legacy_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/monolith"

    # --- New services (HTTP only) -----------------------------------------
    user_service_url: str = "http://localhost:8001"
    sales_service_url: str = "http://localhost:8080"

    # --- Batching --------------------------------------------------------
    batch_size: int = Field(default=100, gt=0)

    # --- HTTP client ----------------------------------------------------
    http_timeout_seconds: float = Field(default=5.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0)
    http_backoff_base_seconds: float = Field(default=0.2, ge=0)

    # --- Local operational state (NOT business data) -------------------
    migration_state_database_url: str = "sqlite:///migration_state.db"

    # --- Rollback safety ----------------------------------------------
    allow_destructive_rollback: bool = False

    # --- Output ------------------------------------------------------
    report_dir: str = "./reports"
    log_format: str = "json"  # json | console
    log_level: str = "INFO"

    def masked_legacy_url(self) -> str:
        """Legacy URL with the password blanked, for logs / reports."""
        url = self.legacy_database_url
        if "@" in url and "//" in url:
            scheme, rest = url.split("//", 1)
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}//{user}:***@{host}"
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
