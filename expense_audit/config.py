"""
expense_audit/config.py
-----------------------
Centralised configuration loaded from environment variables (or .env file).
Secrets are NEVER hardcoded — all values come from the environment.
"""

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env early so values are available when Settings is instantiated.
load_dotenv()


class Settings(BaseSettings):
    """Application settings — read from environment variables only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # LLM / API keys
    # ------------------------------------------------------------------
    google_api_key: str = Field(
        default="",
        description="Google Gemini API key. Required only for full LLM pipeline.",
    )

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    pseudonym_salt: str = Field(
        default="",
        description="HMAC salt for employee-ID pseudonymisation. Set to any random secret.",
    )

    # ------------------------------------------------------------------
    # MCP / Drive export
    # ------------------------------------------------------------------
    google_drive_mcp_credentials: str = Field(
        default="",
        description="Path to Drive MCP service-account JSON. Leave empty to disable Drive export.",
    )

    # ------------------------------------------------------------------
    # API server
    # ------------------------------------------------------------------
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO")

    # ------------------------------------------------------------------
    # Model selection
    # ------------------------------------------------------------------
    gemini_model: str = Field(
        default="gemini-2.0-flash",
        description="Gemini model used by all LlmAgents.",
    )

    # ------------------------------------------------------------------
    # Policy limits (can be overridden via env for different departments)
    # ------------------------------------------------------------------
    limit_meals: float = Field(default=50.0)
    limit_travel: float = Field(default=1500.0)
    limit_lodging: float = Field(default=300.0)
    limit_office_supplies: float = Field(default=200.0)
    limit_client_entertainment: float = Field(default=250.0)
    limit_software_subscriptions: float = Field(default=100.0)

    # Approval threshold
    approval_threshold: float = Field(default=500.0)

    # Fraud detection tuning
    round_number_min_count: int = Field(
        default=3,
        description="Minimum round-dollar submissions by one employee before flagging.",
    )
    threshold_skirting_lower: float = Field(default=450.0)
    threshold_skirting_upper: float = Field(default=499.99)
    duplicate_window_days: int = Field(
        default=7,
        description="Time window in days for near-duplicate detection.",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @property
    def category_limits(self) -> dict[str, float]:
        return {
            "Meals": self.limit_meals,
            "Travel": self.limit_travel,
            "Lodging": self.limit_lodging,
            "Office Supplies": self.limit_office_supplies,
            "Client Entertainment": self.limit_client_entertainment,
            "Software/Subscriptions": self.limit_software_subscriptions,
        }

    @property
    def drive_mcp_enabled(self) -> bool:
        return bool(self.google_drive_mcp_credentials)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.google_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    settings = Settings()
    logging.basicConfig(level=getattr(logging, settings.log_level))
    return settings
