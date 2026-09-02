"""SpaceXAI gateway — LLM provider via xAI OpenAI-compatible API (Grok)."""

from __future__ import annotations

from typing import Any


def _fetch_models(api_key: str, **kw: Any) -> Any:
    from .model_fetch import fetch_models

    return fetch_models(api_key, verify=bool(kw.get("verify", False)))


def register(api) -> None:
    from .model_fetch import clear_model_cache
    from .spacexai_provider import SpaceXAIProvider

    api.register_llm_provider(
        "spacexai",
        factory=lambda api_key, model, **kw: SpaceXAIProvider(api_key, model, **kw),
        fetch_models=_fetch_models,
        test_key_model="grok-4.5",
        tool_schema="openai",
        clear_model_cache=clear_model_cache,
    )
    api.log("SpaceXAI gateway contribution active (Providers)")
