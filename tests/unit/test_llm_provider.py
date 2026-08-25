"""Provider-layer units: credential detection and Anthropic wire translation.

Neither needs a database or a network. Both cover paths that cannot be reached
by the evaluation suite in this build — there are no credentials, so the
Anthropic provider never runs — which is exactly why they are tested here
rather than assumed to work.
"""
from __future__ import annotations

import pytest

from app import config
from app.config import Settings, detect_anthropic_credentials
from app.llm.anthropic_provider import wire_messages

_CRED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_IDENTITY_TOKEN",
)


@pytest.fixture
def clean_env(monkeypatch):
    """No credentials in the environment, and no CLI probe by default."""
    for var in _CRED_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_cli_profile_is_active", lambda: False)
    detect_anthropic_credentials.cache_clear()
    yield monkeypatch
    detect_anthropic_credentials.cache_clear()


# ------------------------------------------------------- credential detection
def test_no_credentials_detected(clean_env):
    assert detect_anthropic_credentials() is None


@pytest.mark.parametrize("var,expected", [
    ("ANTHROPIC_API_KEY", "api_key"),
    ("ANTHROPIC_AUTH_TOKEN", "auth_token"),
])
def test_env_credentials_are_detected(clean_env, var, expected):
    clean_env.setenv(var, "x")
    detect_anthropic_credentials.cache_clear()
    assert detect_anthropic_credentials() == expected


def test_workload_identity_needs_every_variable(clean_env):
    for var in ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
                "ANTHROPIC_SERVICE_ACCOUNT_ID"):
        clean_env.setenv(var, "x")
    detect_anthropic_credentials.cache_clear()
    # Three of four: the SDK could not exchange a token, so neither do we.
    assert detect_anthropic_credentials() is None

    clean_env.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", "/tmp/token")
    detect_anthropic_credentials.cache_clear()
    assert detect_anthropic_credentials() == "workload_identity"


def test_cli_profile_counts_as_a_credential(clean_env):
    """The gap this closes: `ant auth login` with no environment variable set.

    A zero-argument `anthropic.Anthropic()` authenticates from that profile, so
    resolving to the deterministic planner would be a silent downgrade.
    """
    assert detect_anthropic_credentials() is None
    clean_env.setattr(config, "_cli_profile_is_active", lambda: True)
    detect_anthropic_credentials.cache_clear()
    assert detect_anthropic_credentials() == "cli_profile"


def test_auto_resolves_to_anthropic_when_only_a_cli_profile_exists(clean_env):
    s = Settings(llm_provider="auto", anthropic_api_key=None)
    assert s.resolved_llm_provider == "deterministic"

    clean_env.setattr(config, "_cli_profile_is_active", lambda: True)
    detect_anthropic_credentials.cache_clear()
    assert Settings(llm_provider="auto",
                    anthropic_api_key=None).resolved_llm_provider == "anthropic"


def test_explicit_provider_overrides_detection(clean_env):
    """Reproducible evaluation must stay available on a machine with credentials."""
    clean_env.setattr(config, "_cli_profile_is_active", lambda: True)
    detect_anthropic_credentials.cache_clear()
    s = Settings(llm_provider="deterministic", anthropic_api_key=None)
    assert s.resolved_llm_provider == "deterministic"


def test_probe_is_skipped_when_disabled(clean_env):
    clean_env.setattr(config.shutil, "which", lambda _: "/usr/bin/ant")
    clean_env.setenv("MERCHANTOPS_NO_CLI_AUTH_PROBE", "1")
    assert config._cli_profile_is_active() is False


# ----------------------------------------------------------- wire translation
def _conversation() -> list[dict]:
    return [
        {"role": "user", "content": "Why did revenue drop?"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "get_revenue_summary", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "{...}",
             "_structured": {"success": True}}]},
    ]


def test_runtime_annotations_never_reach_the_wire():
    """`_structured` is how the deterministic planner reads a tool result. The
    Messages API rejects unknown fields in a content block, so it must be
    stripped — a path no scenario can exercise without credentials."""
    wired = wire_messages(_conversation())
    assert all(not k.startswith("_")
               for m in wired if isinstance(m["content"], list)
               for block in m["content"] for k in block)


def test_cache_breakpoint_lands_on_the_last_block_only():
    wired = wire_messages(_conversation())
    marked = [block for m in wired if isinstance(m["content"], list)
              for block in m["content"] if "cache_control" in block]
    assert len(marked) == 1
    assert marked[0]["tool_use_id"] == "tu_1"


def test_wire_messages_does_not_mutate_the_runtime_list():
    """The loop reuses its message list across turns. A breakpoint left behind
    would accumulate until the request exceeded the four the API allows."""
    original = _conversation()
    wire_messages(original)
    assert "cache_control" not in original[-1]["content"][0]
    assert original[-1]["content"][0]["_structured"] == {"success": True}


def test_string_content_is_passed_through_unchanged():
    wired = wire_messages([{"role": "user", "content": "Why did revenue drop?"}])
    assert wired == [{"role": "user", "content": "Why did revenue drop?"}]
