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

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_requests)


class LLMProvider(ABC):
    name: str = "abstract"
    model: str = "abstract"

    @abstractmethod
    def turn(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn: ...
