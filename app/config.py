"""Application configuration. Secrets come from the environment only (CONTRACT §37)."""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://merchantops:merchantops@127.0.0.1:5432/merchantops"

    # --- LLM (CONTRACT §44: provider abstraction) ---
    llm_provider: str = "auto"          # auto | anthropic | deterministic
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 8000
    llm_effort: str = "medium"
    anthropic_api_key: str | None = None

    # --- Agent execution budget (CONTRACT §10) ---
    max_tool_calls_per_task: int = 12
    max_llm_turns_per_task: int = 8
    max_wall_clock_seconds: int = 60

    # --- Razorpay (CONTRACT §7, §22) ---
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_mode: str = "auto"         # auto | live_test_mode | mock

    # --- Policy (CONTRACT §20) ---
    refund_amount_limit_minor: int = 5_000_00   # paise
    approval_ttl_seconds: int = 900             # CONTRACT §21 expiration

    agent_version: str = "merchantops-agent/0.1.0"
    prompt_version: str = "investigator-v1"

    @property
    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        key = self.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
        return "anthropic" if key else "deterministic"

    @property
    def resolved_razorpay_mode(self) -> str:
        if self.razorpay_mode != "auto":
            return self.razorpay_mode
        return "live_test_mode" if (self.razorpay_key_id and self.razorpay_key_secret) else "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
