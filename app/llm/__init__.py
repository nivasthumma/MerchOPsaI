from app.config import get_settings
from app.llm.base import LLMProvider, LLMTurn, ToolRequest


def get_provider() -> LLMProvider:
    name = get_settings().resolved_llm_provider
    if name == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    from app.llm.deterministic import DeterministicProvider
    return DeterministicProvider()


__all__ = ["LLMProvider", "LLMTurn", "ToolRequest", "get_provider"]
