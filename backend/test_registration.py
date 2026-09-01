from __future__ import annotations

from typing import Any

from backend import register
from backend.grok_build_adapter import GrokBuildAdapter


class _Api:
    def __init__(self) -> None:
        self.providers: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.logs: list[str] = []

    def register_llm_provider(self, provider_id: str, **kwargs: Any) -> None:
        self.providers[provider_id] = kwargs

    def register_coding_agent(self, agent_id: str, **kwargs: Any) -> None:
        self.agents[agent_id] = kwargs

    def log(self, message: str) -> None:
        self.logs.append(message)


def test_registers_provider_and_grok_build_agent() -> None:
    api = _Api()
    register(api)

    assert "spacexai" in api.providers
    assert "grok_build" in api.agents
    registration = api.agents["grok_build"]
    assert isinstance(registration["factory"](), GrokBuildAdapter)
    assert registration["token_provider"] == "spacexai"
    assert registration["native_skills"] is True
    assert registration["aliases"] == ["grok", "grok_cli"]
    assert registration["normalize_model"]("") == "default"
    assert registration["thinking_env"]("high") == {
        "DUCKY_GROK_REASONING_EFFORT": "high"
    }
    assert any("Grok Build" in message for message in api.logs)
