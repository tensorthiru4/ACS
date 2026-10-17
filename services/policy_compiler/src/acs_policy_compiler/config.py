"""Runtime configuration from `ACS_*` environment variables (see infra/terraform/agents.tf)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACS_", env_file=".env", extra="ignore")

    env: str = "local"
    project_id: str = ""
    firestore_database: str = "(default)"
    # This service's own URL, advertised in the agent card (the orchestrator pins it anyway).
    self_url: str = "http://localhost:8081"
    gemini_model: str = "gemini-3.8-flash"
    # Only the orchestrator delegates to the compiler.
    allowed_callers: list[str] = Field(default_factory=list)
    # Gemini attempts per compile: the first draft plus repairs fed with validation errors.
    max_attempts: int = Field(default=2, ge=1, le=3)
    draft_ttl_days: int = 30
    # Four-eyes: refuse a deploy approved by the same user who compiled the draft.
    require_distinct_approver: bool = False

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
