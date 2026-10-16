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

    # "Stores Raw PO"
    raw_po_bucket: str = ""
    # "Validates Terms" (empty: every draft goes to human review)
    rule_engine_url: str = ""
    # "Draft Sales Order" -> ERP Bridge + Safety Gate (Cloud Workflows); empty disables posting
    erp_workflow: str = ""

    gemini_model: str = "gemini-3.8-flash"
    # Must match the Firestore vector index on skus.embedding (infra/terraform/data.tf).
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768

    # Route allow-lists (Cloud Run IAM is the primary gate).
    push_callers: list[str] = Field(default_factory=list)  # acs-pubsub-push@
    orchestrator_callers: list[str] = Field(default_factory=list)  # acs-orchestrator@

    max_po_bytes: int = 8 * 1024 * 1024
    # One delivery owns an intake for this long; duplicates arriving meanwhile are retried.
    lease_s: int = 600
    # Matches the subscription's dead-letter policy; the last attempt marks the intake failed.
    max_delivery_attempts: int = 5
    # Cosine distance at or below which a vector match is accepted, and the gap required
    # to the runner-up so near-ties go to a human. Calibrated on gemini-embedding-001 at
    # 768 dims: true matches ~0.20-0.27, ambiguous near-ties < 0.01 apart.
    sku_max_distance: float = 0.30
    sku_min_margin: float = 0.03
    default_currency: str = "USD"
    downstream_timeout_s: float = 30.0

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
