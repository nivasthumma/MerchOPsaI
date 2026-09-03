"""LLM provider abstraction — CONTRACT §44.

Kept deliberately thin: one interface, one turn, no framework. The agent loop
owns control flow because policy interception, budget enforcement, trace
persistence and frozen-tool replay all need to sit between the model's tool
request and its execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolRequest:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMTurn:
    """One assistant turn."""
    text: str = ""
    tool_requests: list[ToolRequest] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw: Any = None
    usage: dict = field(default_factory=dict)

    # The assistant content to replay verbatim on the next request, in wire
    # form and in the order the model produced it.
    #
    # The loop used to rebuild this from `text` and `tool_requests`, which is
    # lossy in a way that matters: it silently dropped every `thinking` block.
    # Claude Opus 5 runs adaptive thinking by default, and thinking blocks must
    # be echoed back unchanged when the conversation continues on the same
    # model -- on precisely the turns that carry tool calls. A reconstruction
    # cannot round-trip a block it never saw, so the provider hands back what
    # to send instead of the loop guessing.
    #
    # Empty for providers with nothing to preserve (the deterministic planner),
    # and the loop falls back to reconstruction for those.
    echo_blocks: list[dict] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_requests)


class LLMProvider(ABC):
    name: str = "abstract"
    model: str = "abstract"

    @abstractmethod
    def turn(self, *, system: str, messages: list[dict], tools: list[dict],
             timeout: float | None = None) -> LLMTurn:
        """One assistant turn, bounded by `timeout` seconds.

        The budget belongs to the loop, but only the provider can enforce it
        inside a call. Without this the wall-clock check between turns is the
        only limit there is, and a single request that hangs runs for as long as
        the transport allows — holding a database transaction open the whole
        time. A provider that does no I/O may ignore it.
        """
