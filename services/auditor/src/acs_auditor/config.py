"""Runtime configuration from `ACS_*` environment variables (see infra/terraform/agents.tf)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACS_", env_file=".env", extra="ignore")

    env: str = "local"
    project_id: str = ""
    region: str = "us-central1"
    firestore_database: str = "(default)"
    # This service's URL: agent card, Cloud Tasks target and OIDC audience.
    self_url: str = "http://localhost:8082"
    gemini_model: str = "gemini-3.8-flash"

    # A2A callers (orchestrator) and audit-run callers (Cloud Tasks, as this service's SA).
    allowed_callers: list[str] = Field(default_factory=list)
    runner_callers: list[str] = Field(default_factory=list)

    # Cloud Tasks queue that runs audits inside a request (CPU is only allocated while a
    # request is in flight). Empty: run in-process (local development only).
    tasks_queue: str = ""
    tasks_service_account: str = ""

    bigquery_dataset: str = "acs"
    page_timeout_s: float = 20.0
    # Hard cap on one audit's browsing time (Cloud Run timeout for the auditor is 900 s).
    audit_budget_s: float = 600.0

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
