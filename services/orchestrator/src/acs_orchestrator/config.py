"""Runtime configuration, loaded from environment variables (12-factor).

Secrets are never hard-coded: on Cloud Run they are injected from Secret Manager
as environment variables (see infra/terraform/cloud_run.tf).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACS_", env_file=".env", extra="ignore")

    env: Env = Env.LOCAL
    app_name: str = "acs_orchestrator"
    project_id: str = Field(default="", description="GCP project id")
    region: str = "us-central1"
    # Public base URL of the API Gateway / Firebase Hosting front door. Used in the
    # UCP profile and the A2A agent card so clients never see the raw run.app URL.
    public_base_url: str = "http://localhost:8080"

    # --- Model (Gemini via Google AI Studio free tier, key injected from Secret Manager)
    gemini_model: str = "gemini-3.8-flash"
    # Fallback when Gemini returns 429/5xx: Gemma on the same AI Studio key, which has a
    # separate free-tier quota. Empty turns the fallback off.
    fallback_model: str = "gemma-4-31b-it"

    # --- Downstream agents (Cloud Run URLs; also used as ID-token audiences)
    auditor_url: str = ""
    policy_compiler_url: str = ""
    rule_engine_url: str = ""
    # Messy-Order-to-ERP agent: human release/reject of draft sales orders.
    messy_order_url: str = ""

    # --- Data plane
    firestore_database: str = "(default)"
    po_intake_topic: str = "po-intake-events"
    session_ttl_days: int = 7

    # --- Front-door authentication
    firebase_project_id: str = ""
    firebase_check_revoked: bool = True
    # Audience Pub/Sub uses when it signs push requests for the Gmail webhook.
    gmail_push_audience: str = ""
    # Service account e-mail that Pub/Sub signs Gmail push tokens as.
    gmail_push_service_account: str = ""
    # Pepper for hashing API client secrets (Secret Manager -> env).
    api_key_pepper: SecretStr = SecretStr("")
    # Gmail OAuth JSON {client_id, client_secret, refresh_token} (Secret Manager -> env).
    gmail_oauth_json: SecretStr = SecretStr("")
    # Only mail from these sender domains becomes a purchase order.
    gmail_allowed_sender_domains: list[str] = Field(default_factory=list)
    # Merchant (tenant) that owns the monitored B2B inbox.
    gmail_merchant_id: str = ""
    # Mailbox address that Gmail push notifications are accepted for.
    gmail_mailbox: str = ""

    # --- Guard rails
    cors_allowed_origins: list[str] = Field(default_factory=list)
    max_request_bytes: int = 9 * 1024 * 1024
    # Pub/Sub messages are capped at 10 MB; keep headroom for attributes.
    max_po_bytes: int = 8 * 1024 * 1024
    max_chat_chars: int = 4000
    rate_limit_per_minute: int = 60
    downstream_timeout_s: float = 120.0

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _prod_requirements(self) -> Settings:
        if self.env == Env.PROD:
            missing = [
                name
                for name, value in {
                    "ACS_PROJECT_ID": self.project_id,
                    "ACS_FIREBASE_PROJECT_ID": self.firebase_project_id,
                    "ACS_API_KEY_PEPPER": self.api_key_pepper.get_secret_value(),
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(f"missing required production settings: {', '.join(missing)}")
            if len(self.api_key_pepper.get_secret_value()) < 32:
                raise ValueError("ACS_API_KEY_PEPPER must be at least 32 characters")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("ACS_PUBLIC_BASE_URL must be https in prod")
        return self

    @property
    def is_local(self) -> bool:
        return self.env == Env.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
