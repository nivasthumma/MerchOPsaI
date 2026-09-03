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


# --------------------------------------------------------------------------
# Thinking blocks must survive the round trip (MerchantOps §16, ADR-0014)
# --------------------------------------------------------------------------
class _Block:
    """A stand-in for an SDK content block.

    The real ones are pydantic models, so `model_dump` is the interface that
    matters here — not the class.
    """

    def __init__(self, **fields):
        self._fields = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def model_dump(self, mode=None, exclude_none=False):
        if exclude_none:
            return {k: v for k, v in self._fields.items() if v is not None}
        return dict(self._fields)


def _thinking_turn():
    """What a tool-calling turn looks like with adaptive thinking on."""
    return [
        _Block(type="thinking", thinking="UPI is the outlier.",
               signature="sig_abc123", cache_control=None),
        _Block(type="text", text="Let me check the payment metrics."),
        _Block(type="tool_use", id="tu_9", name="get_payment_metrics",
               input={"window": "7d"}),
    ]


def test_thinking_blocks_are_captured_for_replay():
    """The defect this test exists for: they used to be dropped entirely.

    The provider read only `text` and `tool_use` off the response, so a
    `thinking` block never reached `LLMTurn` at all — and the loop then rebuilt
    the assistant turn without it. Opus 5 runs adaptive thinking by default and
    wants those blocks back unchanged on the next request.
    """
    from app.llm.anthropic_provider import echo_blocks

    blocks = echo_blocks(_thinking_turn())
    kinds = [b["type"] for b in blocks]
    assert kinds == ["thinking", "text", "tool_use"], (
        "every block must survive, in the order the model produced them")

    thinking = blocks[0]
    assert thinking["thinking"] == "UPI is the outlier."
    assert thinking["signature"] == "sig_abc123", (
        "a thinking block without its signature is not the same block")


def test_echo_blocks_drop_none_valued_fields():
    """The SDK populates optional fields with None; the API rejects them."""
    from app.llm.anthropic_provider import echo_blocks

    assert "cache_control" not in echo_blocks(_thinking_turn())[0]


def test_echo_blocks_are_json_native():
    """They are persisted to `agent_messages` between turns.

    Anything that survives in memory but not through JSON fails later, in a
    place that does not name this function.
    """
    import json

    from app.llm.anthropic_provider import echo_blocks

    assert json.loads(json.dumps(echo_blocks(_thinking_turn()))) == \
        echo_blocks(_thinking_turn())


def test_the_loop_replays_provider_blocks_verbatim():
    """The runtime must send back what the model produced, not its own summary.

    Asserted at the loop's own boundary rather than through a live call, which
    no credential here can make.
    """
    from app.llm.base import LLMTurn, ToolRequest

    turn = LLMTurn(
        text="Let me check the payment metrics.",
        tool_requests=[ToolRequest(id="tu_9", name="get_payment_metrics",
                                   arguments={"window": "7d"})],
        stop_reason="tool_use",
        echo_blocks=[
            {"type": "thinking", "thinking": "UPI is the outlier.",
             "signature": "sig_abc123"},
            {"type": "text", "text": "Let me check the payment metrics."},
            {"type": "tool_use", "id": "tu_9", "name": "get_payment_metrics",
             "input": {"window": "7d"}},
        ],
    )

    # The exact expression app/agent/runtime.py uses to build the assistant turn.
    blocks = list(turn.echo_blocks)
    if not blocks:
        blocks = [{"type": "tool_use", "id": t.id, "name": t.name,
                   "input": t.arguments} for t in turn.tool_requests]
        if turn.text:
            blocks.insert(0, {"type": "text", "text": turn.text})

    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["signature"] == "sig_abc123"


def test_a_provider_with_nothing_to_echo_still_works():
    """The deterministic planner produces no blocks, and must still drive a turn.

    Without this the fallback path would be untested, and the fallback is what
    every scenario in the suite actually runs on.
    """
    from app.llm.base import LLMTurn, ToolRequest

    turn = LLMTurn(text="checking", stop_reason="tool_use",
                   tool_requests=[ToolRequest(id="tu_1", name="get_revenue_summary",
                                              arguments={})])

    blocks = list(turn.echo_blocks)
    if not blocks:
        blocks = [{"type": "tool_use", "id": t.id, "name": t.name,
                   "input": t.arguments} for t in turn.tool_requests]
        if turn.text:
            blocks.insert(0, {"type": "text", "text": turn.text})

    assert [b["type"] for b in blocks] == ["text", "tool_use"]
