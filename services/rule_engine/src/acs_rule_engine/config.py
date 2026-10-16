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
    # Service accounts allowed to call /v1/evaluate (orchestrator, messy-order agent,
    # policy compiler). Cloud Run IAM is the primary gate; this is defense in depth.
    allowed_callers: list[str] = Field(default_factory=list)
    # Compiled rule sets are cached per instance; a deploy is live everywhere within this.
    cache_ttl_s: int = 30
    # Local development only: JSON file of rule-set documents to serve from memory.
    seed_rules_file: str = ""

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
