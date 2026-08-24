"""Anthropic provider — the real reasoning path."""
from __future__ import annotations

import json

from app.config import get_settings
from app.llm.base import LLMProvider, LLMTurn, ToolRequest


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic
        s = get_settings()
        self.model = s.llm_model
        self._s = s
        # Zero-arg construction resolves ANTHROPIC_API_KEY or an active
        # `ant auth login` profile.
        self._client = anthropic.Anthropic(api_key=s.anthropic_api_key) if s.anthropic_api_key \
            else anthropic.Anthropic()

    def turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self._s.llm_max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": self._s.llm_effort},
        )

        # A refusal is a real stop_reason on current models; never read content
        # before checking it.
        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            return LLMTurn(text=f"[model declined: {getattr(detail, 'category', 'unspecified')}]",
                           stop_reason="refusal", raw=resp)

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
            usage={"input_tokens": resp.usage.input_tokens,
                   "output_tokens": resp.usage.output_tokens},
        )
