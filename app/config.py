"""Application configuration. Secrets come from the environment only (CONTRACT §37)."""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# `functions["api/index.py"].maxDuration` in vercel.json. Duplicated rather than
# read from that file because the deployment config is not importable at
# runtime, and a budget that silently exceeds it is the failure this pair
# exists to prevent. Change one, change the other.
VERCEL_MAX_DURATION_SECONDS = 60

# Headroom between the agent's deadline and the host's. The loop still has to
# validate its output, write a closing audit event, commit and serialise a
# response after the last turn returns; if the host's axe falls during that, the
# run is lost as completely as if the budget had never been checked.
PLATFORM_MARGIN_SECONDS = 10


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
# Shared across replicas when REDIS_URL is set, and process-local when it is
# not. Process-local was the whole story until ADR-0044, and it made this switch
# quietly wrong with more than one worker: an operator switching to the
# deterministic planner switched it for whichever replica served the POST, and
# then watched the model keep being used by the other two.
#
# The process-local copy is still written on both paths. It is the fallback when
# there is no Redis, and it also means a replica that just set the value behaves
# correctly for the rest of the request even if Redis drops immediately after.
_runtime_provider: str | None = None


def set_runtime_llm_provider(value: str | None) -> bool:
    """Set the override. Returns True when it reached shared state.

    The caller reports that: an operator who switched providers across a fleet
    and an operator who switched them for one replica should not receive the
    same response.
    """
    global _runtime_provider
    _runtime_provider = value

    from app import shared_state

    return shared_state.set_provider_override(value)


def runtime_llm_provider() -> str | None:
    """The active override.

    Shared state wins when it can be read. `UNAVAILABLE` is not `None`: a Redis
    that cannot be reached must not read as "somebody cleared the override",
    which would silently switch a fleet back to its configured provider in the
    middle of an incident. That case falls through to this process's own copy,
    which is the last value this replica knew.
    """
    from app import shared_state

    value = shared_state.get_provider_override()
    if value is shared_state.UNAVAILABLE:
        return _runtime_provider
    return value


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
    # A ceiling on any single model call. The loop passes its own remaining
    # budget per turn; this bounds the case where that is still too generous.
    # Both are deadlines for the whole call, divided across attempts by the
    # provider -- see AnthropicProvider._attempt_timeout.
    llm_timeout_seconds: int = 30
    # Transient provider failures are worth absorbing (§57 grades a 5xx
    # BOUNDED_BACKOFF). Kept as a setting because it and the timeout above are
    # one budget between them, and tuning either alone gets that wrong.
    llm_max_retries: int = 2

    # --- Agent execution budget (CONTRACT §10) ---
    max_tool_calls_per_task: int = 12
    max_llm_turns_per_task: int = 8
    max_wall_clock_seconds: int = 60
    # What the host allows one invocation, which is not the same question.
    # Defaults to Vercel's configured `maxDuration` when running there; None
    # means nothing outside the process is holding a stopwatch.
    # KEEP IN STEP WITH vercel.json.
    platform_timeout_seconds: int | None = Field(
        default_factory=lambda: VERCEL_MAX_DURATION_SECONDS if os.getenv("VERCEL") else None)

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

    # --- Operator notifications ---------------------------------------
    # The channels a notification may go out on, in order of preference. `log`
    # is always available and is the default: a deployment with no SMTP still
    # records every notification somewhere a human can read, rather than
    # dropping it. A name here that is not configured is a startup error, not a
    # silent fallback -- "we thought we were emailing" is the failure this
    # package exists to prevent.
    notify_channels: str = "log"
    notify_timeout_seconds: float = 10.0
    # How long before an approval expires to chase it. Must be less than
    # `approval_ttl_seconds` or the chase never fires; asserted at startup.
    notify_approval_warning_seconds: int = 300
    # Below this severity nothing is sent. INFO | WARNING | CRITICAL.
    notify_min_severity: str = "INFO"
    # Where the links in a notification point. Unset means relative paths,
    # which are useless in an email -- so a deployment that sends email
    # should set this, and one that does not costs nothing by leaving it.
    notify_base_url: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    notify_email_from: str | None = None

    slack_webhook_url: str | None = None

    # A JSON POST to a URL the customer owns -- their PagerDuty, Opsgenie or
    # ticket queue. Signed with `notify_webhook_secret` when one is set, the
    # same shape as the Razorpay signature we verify inbound.
    notify_webhook_url: str | None = None
    notify_webhook_secret: str | None = None

    # --- Worker cadence (app/worker.py) --------------------------------
    # How often each sweep runs. These are the numbers that decide how quickly
    # the system reacts to anything nobody is watching, and each one is a
    # trade-off against database load rather than a default worth copying.
    #
    # drain: events sit PENDING until delivered, and the notification consumers
    # hang off that delivery -- so this is the latency between an approval being
    # raised and the approver hearing about it. Cheap: one indexed query.
    worker_drain_interval_seconds: int = 5
    # notify: MUST be well under `notify_approval_warning_seconds` (300), or the
    # chase for an expiring approval is delivered after the window it was
    # warning about has closed. Sending is deduplicated by a UNIQUE constraint,
    # so running this more often costs queries and sends nothing twice.
    worker_notify_interval_seconds: int = 60
    # reconcile: re-reads provider state for unsettled actions. Bounded by an
    # outbound call per action, so it is the expensive one.
    worker_reconcile_interval_seconds: int = 300
    # detect: incidents appear at this cadence and no faster. The README states
    # that trade-off; this is the number behind it.
    worker_detect_interval_seconds: int = 300
    # tasks: how long a queued task waits before a worker picks it up. This is
    # the latency a person feels after pressing the button, so it is the
    # tightest of these -- and claiming is one indexed UPDATE, so a pass that
    # finds nothing is nearly free.
    worker_tasks_interval_seconds: int = 2
    # How many tasks one pass will run before returning to the other jobs. A
    # task is expensive; without a bound a busy queue would starve the sweeps.
    worker_max_tasks_per_pass: int = 5
    # Must stay well under queue.WORKER_LIVENESS_SECONDS (90), or a live worker
    # reads as dead between beats and `POST /tasks` starts refusing.
    worker_heartbeat_interval_seconds: int = 15

    # --- Agent execution (ADR-0045) ------------------------------------
    # `inline` runs a task inside the request that created it, which is the
    # only thing possible where there is no worker -- Vercel, or a bare
    # `make api`. `async` accepts it and returns 202 with an id to poll.
    #
    # Default `inline` so nothing that works today stops working. The compose
    # stack sets `async`, because it has a worker. `POST /tasks?mode=` overrides
    # per request either way.
    agent_execution_mode: str = "inline"

    agent_version: str = "merchantops-agent/0.1.0"
    prompt_version: str = "investigator-v1"
    # MerchantOps §41. The shape of the loop the agent runs inside: gather,
    # gate, approve, execute, verify. Bumped when that shape changes, not when
    # a step's implementation does.
    workflow_version: str = "workflow-v2"

    @property
    def effective_wall_clock_seconds(self) -> int:
        """The budget actually enforced, which is not always the one configured.

        A budget larger than the host's own timeout is not a budget. The
        invocation is killed part-way, the request's transaction rolls back, and
        the careful ABORTED_BUDGET path — partial trace preserved, failure code
        recorded, operator told why — is the one path that never runs. The two
        numbers lived in different files and disagreed: 60s here against
        `maxDuration: 30` in vercel.json, so every task that used its documented
        budget was killed at half of it.

        The host's limit now caps ours, less a margin to finish the work that
        happens after the loop: the §37 output check, the closing audit event,
        and serialising a response. A configured budget already inside the limit
        is left exactly as it is.
        """
        limit = self.platform_timeout_seconds
        if limit is None:
            return self.max_wall_clock_seconds
        return max(5, min(self.max_wall_clock_seconds, limit - PLATFORM_MARGIN_SECONDS))

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
