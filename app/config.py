"""Application configuration. Secrets come from the environment only (CONTRACT §37)."""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Credential sources the Anthropic SDK resolves, in the order it resolves them.
# An unset ANTHROPIC_API_KEY does NOT mean "no credentials": a zero-argument
# `anthropic.Anthropic()` also picks up an auth token, an `ant auth login`
# profile, or workload-identity federation. Testing only for the key made
# `LLM_PROVIDER=auto` fall back to the deterministic planner on a machine that
# could in fact reach the model — a silent downgrade, and the evaluation report
# would have said `deterministic` with no indication that a choice was made.
_WIF_REQUIRED = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)


def _cli_profile_is_active() -> bool:
    """True when `ant auth status` reports a usable credential.

    Shelling out is the only honest check — the profile store's location and
    format are the CLI's business, not ours. It is cached, guarded by a
    `which`, and bounded by a timeout, so a missing or wedged CLI costs one
    failed probe and never blocks a request.
    """
    if os.getenv("MERCHANTOPS_NO_CLI_AUTH_PROBE"):
        return False
    if shutil.which("ant") is None:
        return False
    try:
        return subprocess.run(["ant", "auth", "status"], capture_output=True,
                              timeout=3.0).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# A runtime override of the configured provider.
#
# Process-local and deliberately not persisted: it survives no restart, and with
# more than one worker each would hold its own. That is a real limitation, and
# the honest place for it is here rather than in a comment on the UI — the same
# constraint the in-process rate limiter carries.
_runtime_provider: str | None = None


def set_runtime_llm_provider(value: str | None) -> None:
    global _runtime_provider
    _runtime_provider = value


def runtime_llm_provider() -> str | None:
    return _runtime_provider


@lru_cache(maxsize=8)
def detect_anthropic_credentials(explicit_key: str | None = None) -> str | None:
    """Which credential source the SDK would use, or None if it has none.

    Returned rather than a bare bool so `/health` and the evaluation report can
    say *why* a provider was selected instead of leaving the reader to guess.
    """
    if explicit_key or os.getenv("ANTHROPIC_API_KEY"):
        return "api_key"
    if os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "auth_token"
    if all(os.getenv(v) for v in _WIF_REQUIRED) and (
            os.getenv("ANTHROPIC_IDENTITY_TOKEN_FILE") or os.getenv("ANTHROPIC_IDENTITY_TOKEN")):
        return "workload_identity"
    if _cli_profile_is_active():
        return "cli_profile"
    return None


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
    # MerchantOps §34. Separate from the API key secret: Razorpay signs webhooks
    # with a secret you choose per endpoint, and reusing the API secret here
    # would mean one leak compromises both directions.
    razorpay_webhook_secret: str | None = None

    # --- Policy (CONTRACT §20) ---
    refund_amount_limit_minor: int = 5_000_00   # paise
    approval_ttl_seconds: int = 900             # CONTRACT §21 expiration

    # --- Recovery budget (MerchantOps §27) -----------------------------
    # The spec's worked example, verbatim: INR 50,000, 500 actions, 2 attempts
    # per customer, 24 hours. These bound a CAMPAIGN and are a different axis
    # from the agent execution budget above, which bounds one task's compute.
    # Conflating them would let a cheap agent run spend an unbounded amount.
    recovery_max_amount_minor: int = 50_000_00
    recovery_max_actions: int = 500
    recovery_max_attempts_per_customer: int = 2
    recovery_max_duration_seconds: int = 86_400
    # MerchantOps §28: stop when expected recovery falls below a threshold.
    # Chasing INR 20 costs more in support load than it returns.
    recovery_min_expected_minor: int = 100_00

    agent_version: str = "merchantops-agent/0.1.0"
    prompt_version: str = "investigator-v1"

    @property
    def anthropic_credential_source(self) -> str | None:
        """`api_key` | `auth_token` | `workload_identity` | `cli_profile` | None."""
        return detect_anthropic_credentials(self.anthropic_api_key)

    @property
    def llm_provider_source(self) -> str:
        """Where the active provider came from: `runtime`, `env` or `auto`."""
        if runtime_llm_provider() is not None:
            return "runtime"
        return "env" if self.llm_provider != "auto" else "auto"

    @property
    def resolved_llm_provider(self) -> str:
        # A runtime override wins, so an operator can switch between providers
        # that are already configured. It cannot conjure one: the API refuses to
        # select `anthropic` when no credential was detected.
        override = runtime_llm_provider()
        if override is not None:
            return override
        # An explicit setting is never second-guessed: `deterministic` must stay
        # selectable on a machine that has credentials, or the evaluation suite
        # could not be run reproducibly there.
        if self.llm_provider != "auto":
            return self.llm_provider
        return "anthropic" if self.anthropic_credential_source else "deterministic"

    @property
    def webhook_verification_enabled(self) -> bool:
        """False when no webhook secret is configured.

        Reported rather than silently assumed either way. Refusing every
        delivery would make the endpoint untestable without a secret; accepting
        every delivery without saying so would be a forgery hole nobody could
        see. `/health` publishes this, and unverified events are stored with
        `signature_valid=False` so they can never be mistaken for verified ones.
        """
        return bool(self.razorpay_webhook_secret)

    @property
    def resolved_razorpay_mode(self) -> str:
        if self.razorpay_mode != "auto":
            return self.razorpay_mode
        return "live_test_mode" if (self.razorpay_key_id and self.razorpay_key_secret) else "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
