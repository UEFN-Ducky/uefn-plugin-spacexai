"""SpaceXAI gateway — Grok API provider + Grok Build coding agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_INSTALL_HELP = (
    "Needs the official Grok Build CLI (`grok` in PowerShell). Install in Windows "
    "PowerShell: irm https://x.ai/cli/install.ps1 | iex. Then run `grok login`, "
    "or save a SpaceXAI API key in Ducky. Restart Ducky and click Detect."
)


def _fetch_models(api_key: str, **kw: Any) -> Any:
    from .model_fetch import fetch_models

    return fetch_models(api_key, verify=bool(kw.get("verify", False)))


def _skills_dir() -> str:
    return str(Path.home() / ".grok" / "skills")


def _thinking_env(effort: str) -> dict[str, str]:
    value = (effort or "").strip().lower()
    if value not in {"low", "medium", "high"}:
        return {}
    return {"DUCKY_GROK_REASONING_EFFORT": value}


def _normalize_model(model: str) -> str:
    return (model or "").strip() or "default"


def register(api) -> None:
    from .grok_build_adapter import GrokBuildAdapter
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
    api.register_coding_agent(
        "grok_build",
        factory=lambda: GrokBuildAdapter(),
        aliases=["grok", "grok_cli"],
        skills_dir=_skills_dir,
        native_skills=True,
        thinking_env=_thinking_env,
        normalize_model=_normalize_model,
        settings_defaults={
            "enabled": True,
            "cli_path": "",
            "default_args": "",
        },
        install_help=_INSTALL_HELP,
        token_provider="spacexai",
        shows_thinking_effort=True,
    )
    api.log("SpaceXAI gateway contribution active (Providers + Grok Build)")
