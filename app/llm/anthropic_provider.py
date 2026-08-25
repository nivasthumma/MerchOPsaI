"""Anthropic provider — the real reasoning path."""
from __future__ import annotations

import json

from app.config import get_settings
from app.llm.base import LLMProvider, LLMTurn, ToolRequest

CACHE_CONTROL = {"type": "ephemeral"}


def wire_messages(messages: list[dict]) -> list[dict]:
    """Translate the runtime's message list into Anthropic wire form.

    Two provider-local concerns, neither of which the agent loop should know
    about:

    1. **Strip runtime-internal annotations.** `_handle_tool` attaches
       `_structured` to each `tool_result` block so a provider can read the tool
       result as data instead of re-parsing rendered text — the deterministic
       planner relies on it. The Messages API rejects unknown fields inside a
       content block, so it must never reach the wire.
    2. **Place the rolling cache breakpoint.** The loop resends the whole
       conversation every turn, so marking the last block makes the next turn's
       request read the prefix from cache rather than re-paying for it. The
       stable prefix (tools + system) carries its own breakpoint in `turn()`.

    The input is copied, never mutated: the runtime reuses its `messages` list
    on the next turn, and a `cache_control` key left behind would accumulate
    breakpoints until the request exceeded the four the API allows.
    """
    out: list[dict] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [{k: v for k, v in b.items() if not k.startswith("_")} for b in content]
        out.append({**m, "content": content})

    # A string `content` (the opening user request) is left alone. Wrapping it
    # into a block purely to hold a breakpoint would cache a prefix that the
    # system + tools breakpoint already covers.
    if out and isinstance(out[-1]["content"], list) and out[-1]["content"]:
        last = out[-1]["content"][-1]
        out[-1]["content"][-1] = {**last, "cache_control": CACHE_CONTROL}
    return out


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic
        s = get_settings()
        self.model = s.llm_model
        self._s = s
        # Zero-arg construction resolves whatever the SDK finds: an auth token,
        # an `ant auth login` profile, or workload identity. `Settings.
        # anthropic_credential_source` reports which of those was detected.
        self._client = anthropic.Anthropic(api_key=s.anthropic_api_key) if s.anthropic_api_key \
            else anthropic.Anthropic()

    def turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self._s.llm_max_tokens,
            # Caching is a prefix match over tools -> system -> messages. Both
            # are byte-stable across every turn of every task (the prompt is
            # versioned, the registry is a literal), so this breakpoint is the
            # cheap one: it is re-read on each of up to 8 turns per task.
            system=[{"type": "text", "text": system, "cache_control": CACHE_CONTROL}],
            messages=wire_messages(messages),
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": self._s.llm_effort},
        )

        # A refusal is a real stop_reason on current models; never read content
        # before checking it.
        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            return LLMTurn(text=f"[model declined: {getattr(detail, 'category', 'unspecified')}]",
                           stop_reason="refusal", raw=resp, usage=_usage(resp))

        text_parts, reqs = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # Tool inputs must be treated as parsed JSON, never string-matched.
                args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                reqs.append(ToolRequest(id=block.id, name=block.name, arguments=args))

        return LLMTurn(
            text="\n".join(text_parts).strip(),
            tool_requests=reqs,
            stop_reason=resp.stop_reason or "end_turn",
            raw=resp,
            usage=_usage(resp),
        )


def _usage(resp) -> dict:
    """Token usage, including the two cache counters.

    These are recorded on the `llm_turn` audit event so a claim that caching
    works can be checked against a trace instead of asserted. A
    `cache_read_input_tokens` that stays at 0 across turns means something in
    the prefix is varying.
    """
    u = resp.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
    }
