"""Minimal UEFN-Ducky host stand-ins for this plugin's standalone tests."""

from __future__ import annotations

import enum
import shutil
import sys
import types
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderMessage:
    role: str
    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_id: str = ""
    attachments: list[Any] = field(default_factory=list)


class StreamEventKind(enum.Enum):
    TEXT_DELTA = "text_delta"
    THINKING = "thinking"
    TOOL_CALLS = "tool_calls"
    DONE = "done"


@dataclass
class StreamEvent:
    kind: StreamEventKind
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class PromptCachePayload:
    system: str = ""
    prompt_cache_key: str = ""


@dataclass
class ModelInfo:
    id: str
    display_name: str = ""
    supports_vision: bool = False
    supports_tools: bool = False
    context_limit: int | None = None


@dataclass
class CodingAgentCapabilities:
    terminal_agent: bool = False
    chat_api: bool = False
    a2a: bool = True
    mcp_inject: bool = False
    needs_api_key: bool = False
    needs_cli: bool = False
    resume: bool = False


@dataclass
class CodingAgentInfo:
    id: str
    label: str
    enabled: bool
    available: bool
    status: str
    cli_path: str = ""
    default_args: str = ""
    capabilities: Any = None
    models: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CodingAgentLaunchResult:
    ok: bool
    terminal_session_id: str = ""
    upstream_session_id: str = ""
    output_tail: str = ""
    reply_text: str = ""
    error: str = ""
    status: str = ""
    streamed: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)


class ThinkSplitter:
    def feed(self, text: str) -> list[tuple[str, str]]:
        return [("text", text)] if text else []

    def flush(self) -> list[tuple[str, str]]:
        return []


def _package(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    parent, _, leaf = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], leaf, module)
    return module


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = _package(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _coding_agent_cfg(settings: Any, agent_id: str) -> dict[str, Any]:
    rows = getattr(settings, "coding_agents", {}) or {}
    row = rows.get(agent_id) if isinstance(rows, dict) else None
    return dict(row) if isinstance(row, dict) else {}


def _install_host_stubs() -> None:
    for package in (
        "backend.agent",
        "backend.agent.providers",
        "backend.agent.coding_agents",
        "backend.panel",
    ):
        _package(package)
    _module(
        "backend.agent.providers.base",
        ProviderMessage=ProviderMessage,
        StreamEvent=StreamEvent,
        StreamEventKind=StreamEventKind,
        ToolCallRequest=ToolCallRequest,
    )
    _module(
        "backend.agent.providers.cache_utils",
        openai_system_messages=lambda cache, fallback_system="": (
            [
                {
                    "role": "system",
                    "content": getattr(cache, "system", "") or fallback_system,
                }
            ]
            if (getattr(cache, "system", "") or fallback_system)
            else []
        ),
        parse_openai_usage=lambda usage: {},
    )
    _module(
        "backend.agent.providers.thinking",
        ThinkSplitter=ThinkSplitter,
        reasoning_from_delta=lambda delta: str(
            getattr(delta, "reasoning_content", "") or ""
        ),
    )
    _module("backend.agent.prompt_cache", PromptCachePayload=PromptCachePayload)
    _module(
        "backend.agent.model_fetch",
        ModelInfo=ModelInfo,
        _cache_put=lambda cache, key, value: cache.__setitem__(key, value),
    )
    _module(
        "backend.agent.multimodal_content",
        build_openai_user_content=lambda text, attachments: text,
    )
    _module(
        "backend.agent.secrets", get_key=lambda name: "", has_key=lambda name: False
    )
    _module(
        "backend.agent.coding_agents.base",
        CodingAgentCapabilities=CodingAgentCapabilities,
        CodingAgentInfo=CodingAgentInfo,
        CodingAgentLaunchResult=CodingAgentLaunchResult,
        which_cli=lambda name, override="": override or (shutil.which(name) or ""),
    )
    _module(
        "backend.agent.coding_agents.cli_shared",
        truncate_tool_result=lambda text, max_chars=4000: (
            str(text or "").strip()[:max_chars]
            + ("…(truncated)" if len(str(text or "").strip()) > max_chars else "")
        ),
    )
    _module(
        "backend.agent.coding_agents.settings_helpers",
        coding_agent_cfg=_coding_agent_cfg,
    )
    _module(
        "backend.agent.coding_agents.proc_exec",
        register_process=lambda conv_id, proc: None,
        unregister_process=lambda conv_id, proc: None,
    )
    _module(
        "backend.panel.rpc",
        panel_rpc=lambda method, params, timeout=30.0: {
            "ok": False,
            "skipped_all": True,
        },
    )


_install_host_stubs()
