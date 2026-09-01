"""Grok Build coding-agent adapter using xAI's official ACP stdio mode."""

from __future__ import annotations

import base64
import json
import mimetypes
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.agent.coding_agents.base import (
    CodingAgentCapabilities,
    CodingAgentInfo,
    CodingAgentLaunchResult,
    which_cli,
)
from backend.agent.coding_agents.cli_shared import truncate_tool_result
from backend.agent.coding_agents.settings_helpers import coding_agent_cfg

from .grok_acp import (
    ACP_PROTOCOL_VERSION,
    AcpCancelled,
    AcpError,
    AcpMethodNotFound,
    AcpProtocolError,
    AcpRemoteError,
    AcpTimeout,
    JsonRpcStdioClient,
    build_grok_argv,
    load_mcp_servers,
    validate_mcp_capabilities,
)

_GROK_INSTALL_PS = r"irm https://x.ai/cli/install.ps1 | iex"
_EFFORT_ENV = "DUCKY_GROK_REASONING_EFFORT"
_PLUGIN_VERSION = "1.1.0"
_HANDSHAKE_TIMEOUT_S = 60.0
_PERMISSION_TIMEOUT_S = 600.0
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGES_BYTES = 20 * 1024 * 1024
_LOGIN_MARKERS = (
    "sign in",
    "signed in",
    "log in",
    "login",
    "authenticate",
    "authentication",
    "cached_token",
    "api key",
    "unauthorized",
    "401",
)
_STALE_SESSION_MARKERS = (
    "session not found",
    "unknown session",
    "no such session",
    "resume",
    "load session",
)

_CLI_MODELS: tuple[dict[str, str], ...] = (
    {
        "id": "default",
        "name": "Grok Build (CLI default)",
        "provider": "Grok Build",
    },
)


class _NeedsLogin(AcpError):
    pass


def _missing_status() -> str:
    return (
        "Grok Build CLI not found — install the official `grok` command in Windows "
        f"PowerShell: {_GROK_INSTALL_PS}. Then run `grok login`, restart Ducky, and click Detect."
    )


def _deadline(turn_deadline: float | None, cap_s: float | None = None) -> float | None:
    cap = time.monotonic() + cap_s if cap_s is not None else None
    if turn_deadline is None:
        return cap
    if cap is None:
        return turn_deadline
    return min(turn_deadline, cap)


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_from_prompt_response(
    response: dict[str, Any], selected_model: str
) -> dict[str, Any]:
    meta = response.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    full_input = _safe_int(usage.get("inputTokens", meta.get("inputTokens")))
    output = _safe_int(usage.get("outputTokens", meta.get("outputTokens")))
    cache_read = _safe_int(usage.get("cachedReadTokens", meta.get("cachedReadTokens")))
    cache_write = _safe_int(usage.get("cacheCreationTokens"))
    uncached_input = max(0, full_input - cache_read - cache_write)
    model = str(meta.get("modelId") or "").strip()
    model_usage = usage.get("modelUsage")
    if not model and isinstance(model_usage, dict) and len(model_usage) == 1:
        model = str(next(iter(model_usage), ""))
    if not model:
        model = (
            selected_model
            if selected_model.lower() not in ("", "default", "auto")
            else "grok-build"
        )

    cost: float | None = None
    incomplete = bool(usage.get("usageIsIncomplete"))
    partial = bool(usage.get("costIsPartial"))
    ticks = usage.get("costUsdTicks")
    if not incomplete and not partial and isinstance(ticks, (int, float)):
        cost = float(ticks) / 10_000_000_000.0

    if not any((full_input, output, cache_read, cache_write, cost is not None)):
        return {}
    return {
        "input_tokens": uncached_input,
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "context_tokens": full_input,
        "cost_usd": cost,
        "num_turns": _safe_int(usage.get("numTurns")),
        "model": model,
    }


def _text_content(block: Any) -> str:
    if not isinstance(block, dict) or block.get("type") != "text":
        return ""
    return str(block.get("text") or "")


def _tool_result_text(tool: dict[str, Any]) -> str:
    parts: list[str] = []
    content = tool.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "content":
                text = _text_content(item.get("content"))
                if text:
                    parts.append(text)
            elif kind == "diff":
                path = str(item.get("path") or "").strip()
                if path:
                    parts.append(f"Updated {path}")
    if parts:
        return truncate_tool_result("\n".join(parts))
    raw = tool.get("rawOutput")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return truncate_tool_result(raw)
    try:
        return truncate_tool_result(json.dumps(raw, ensure_ascii=False))
    except (TypeError, ValueError):
        return truncate_tool_result(str(raw))


class _AcpStream:
    """Map ACP session updates onto Ducky's live and persisted event shapes."""

    def __init__(
        self, conv_id: str, run_id: str, push: Callable[[dict[str, Any]], None]
    ) -> None:
        self.conv_id = conv_id
        self.run_id = run_id
        self.push = push
        self.active = False
        self.session_id = ""
        self.text_parts: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self._seg_text: list[str] = []
        self._seg_thinking: list[str] = []
        self._tools: dict[str, dict[str, Any]] = {}

    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("conv_id", self.conv_id)
        if self.run_id:
            event.setdefault("run_id", self.run_id)
        self.push(event)

    def _emit_text(self, text: str) -> None:
        if not text:
            return
        self.text_parts.append(text)
        self._seg_text.append(text)
        self._emit({"type": "text_delta", "text": text})

    def _emit_thinking(self, text: str) -> None:
        if not text:
            return
        self._seg_thinking.append(text)
        self._emit({"type": "thinking", "text": text})

    def _flush_segments(self) -> None:
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
        self._seg_thinking = []
        text = "".join(self._seg_text).strip()
        if text:
            self.blocks.append({"type": "text", "text": text})
        self._seg_text = []

    def trailing_text(self) -> str:
        return "".join(self._seg_text).strip()

    def finalize_blocks(self) -> list[dict[str, Any]]:
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
        self._seg_thinking = []
        return self.blocks

    @staticmethod
    def _arguments(tool: dict[str, Any]) -> dict[str, Any]:
        raw = tool.get("rawInput")
        if isinstance(raw, dict):
            return dict(raw)
        if raw not in (None, ""):
            return {"input": raw}
        locations = tool.get("locations")
        if isinstance(locations, list):
            paths = [
                str(item.get("path") or "")
                for item in locations
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            ]
            if paths:
                return {"path": paths[0], "paths": paths}
        return {}

    def _open_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        tool_id = (
            str(tool.get("toolCallId") or "").strip() or f"tool-{len(self._tools) + 1}"
        )
        current = self._tools.get(tool_id)
        if current is not None:
            current.update(
                {key: value for key, value in tool.items() if value is not None}
            )
            return current
        self._flush_segments()
        title = str(tool.get("title") or tool.get("kind") or "Grok Build tool").strip()
        entry = dict(tool)
        entry["toolCallId"] = tool_id
        entry["title"] = title
        entry["started_at"] = time.monotonic()
        entry["started_wall"] = time.time()
        entry["arguments"] = self._arguments(entry)
        self._tools[tool_id] = entry
        self._emit(
            {
                "type": "tool",
                "text": f"⚙ {title}",
                "tool": {
                    "name": title,
                    "arguments": entry["arguments"],
                    "status": "pending",
                },
            }
        )
        return entry

    def _finish_tool(self, entry: dict[str, Any], *, failed: bool) -> None:
        tool_id = str(entry.get("toolCallId") or "")
        self._tools.pop(tool_id, None)
        title = str(entry.get("title") or "Grok Build tool")
        arguments = (
            entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {}
        )
        started = float(entry.get("started_at") or 0.0)
        duration_ms = int((time.monotonic() - started) * 1000) if started else 0
        result_text = _tool_result_text(entry)
        status = "error" if failed else "success"
        tool_payload: dict[str, Any] = {
            "name": title,
            "arguments": arguments,
            "status": status,
            "durationMs": duration_ms,
            "result": result_text,
            "hint": "",
        }
        if not failed:
            try:
                from frontend.ui_web.verse_editor.agent_sync import (
                    file_edit_meta_for_stream,
                )

                file_edit = file_edit_meta_for_stream(title, arguments, result_text)
                if file_edit:
                    tool_payload["fileEdit"] = file_edit
            except Exception:  # noqa: BLE001 - optional host UI enrichment
                tool_payload.pop("fileEdit", None)
        block: dict[str, Any] = {
            "type": "tool_call",
            "id": tool_id,
            "name": title,
            "arguments": arguments,
            "started": float(entry.get("started_wall") or 0.0),
            "duration_ms": duration_ms,
            "result": {"ok": not failed, "data": result_text, "hint": ""},
            "status": status,
        }
        if "fileEdit" in tool_payload:
            block["file_edit"] = tool_payload["fileEdit"]
        self.blocks.append(block)
        self._emit(
            {
                "type": "tool_done",
                "text": f"⚙ {title} · {status}",
                "success": not failed,
                "tool": tool_payload,
            }
        )

    def _on_tool_call(self, update: dict[str, Any]) -> None:
        entry = self._open_tool(update)
        status = str(entry.get("status") or "pending").lower()
        if status in ("completed", "failed"):
            self._finish_tool(entry, failed=status == "failed")

    def _on_tool_update(self, update: dict[str, Any]) -> None:
        entry = self._open_tool(update)
        entry.update({key: value for key, value in update.items() if value is not None})
        entry["arguments"] = self._arguments(entry) or entry.get("arguments") or {}
        status = str(entry.get("status") or "").lower()
        if status in ("completed", "failed"):
            self._finish_tool(entry, failed=status == "failed")

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update" or not self.active:
            return
        session_id = str(params.get("sessionId") or "")
        if self.session_id and session_id and session_id != self.session_id:
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = str(update.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            self._emit_text(_text_content(update.get("content")))
        elif kind == "agent_thought_chunk":
            self._emit_thinking(_text_content(update.get("content")))
        elif kind == "tool_call":
            self._on_tool_call(update)
        elif kind == "tool_call_update":
            self._on_tool_update(update)
        elif kind == "plan":
            entries = update.get("entries")
            if isinstance(entries, list) and entries:
                completed = sum(
                    1
                    for item in entries
                    if isinstance(item, dict) and item.get("status") == "completed"
                )
                self._emit(
                    {
                        "type": "status",
                        "text": f"Grok Build plan {completed}/{len(entries)}",
                    }
                )

    def finish_unresolved_tools(self, *, cancelled: bool) -> None:
        note = (
            "Cancelled before the tool finished."
            if cancelled
            else "Turn ended before the tool reported a result."
        )
        for entry in list(self._tools.values()):
            entry["rawOutput"] = note
            self._finish_tool(entry, failed=True)


class _PermissionBroker:
    def __init__(
        self,
        conv_id: str,
        run_id: str,
        push: Callable[[dict[str, Any]], None],
    ) -> None:
        self.conv_id = conv_id
        self.run_id = run_id
        self.push = push

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != "session/request_permission":
            raise AcpMethodNotFound(f"Unsupported ACP client method: {method}")
        options = params.get("options")
        if not isinstance(options, list) or not options:
            return {"outcome": {"outcome": "cancelled"}}
        valid: dict[str, dict[str, Any]] = {}
        rendered: list[dict[str, str]] = []
        descriptions = {
            "allow_once": "Allow this action once.",
            "allow_always": "Allow matching actions for this Grok session.",
            "reject_once": "Block this action once.",
            "reject_always": "Block matching actions for this Grok session.",
        }
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = str(option.get("optionId") or "").strip()
            if not option_id:
                continue
            valid[option_id] = option
            kind = str(option.get("kind") or "").strip()
            rendered.append(
                {
                    "id": option_id,
                    "label": str(option.get("name") or kind.replace("_", " ").title()),
                    "description": descriptions.get(
                        kind, "Apply this permission choice."
                    ),
                }
            )
        if not rendered:
            return {"outcome": {"outcome": "cancelled"}}
        tool = params.get("toolCall")
        if not isinstance(tool, dict):
            tool = {}
        title = str(
            tool.get("title") or tool.get("kind") or "Grok Build action"
        ).strip()
        raw_input = tool.get("rawInput")
        detail = ""
        if raw_input not in (None, "", {}):
            try:
                detail = truncate_tool_result(
                    json.dumps(raw_input, ensure_ascii=False), 800
                )
            except (TypeError, ValueError):
                detail = truncate_tool_result(str(raw_input), 800)
        prompt = f"Allow Grok Build to run: {title}?"
        if detail:
            prompt += f"\n\n{detail}"
        self.push(
            {
                "type": "status",
                "text": "Grok Build is waiting for permission…",
                "conv_id": self.conv_id,
                "run_id": self.run_id,
            }
        )
        payload = {
            "conv_id": self.conv_id,
            "title": "Grok Build permission",
            "questions": [
                {
                    "id": "grok_permission",
                    "prompt": prompt,
                    "options": rendered,
                    "required": True,
                    "allow_multiple": False,
                    "allow_free_text": False,
                }
            ],
        }
        try:
            from backend.panel.rpc import panel_rpc

            answer = panel_rpc("ask_user", payload, timeout=_PERMISSION_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - panel absence/closure must fail closed
            return {"outcome": {"outcome": "cancelled"}}
        if (
            not isinstance(answer, dict)
            or answer.get("error")
            or answer.get("skipped_all")
        ):
            return {"outcome": {"outcome": "cancelled"}}
        answers = answer.get("answers")
        row = answers.get("grok_permission") if isinstance(answers, dict) else None
        selected = row.get("selected") if isinstance(row, dict) else None
        option_id = str(
            selected[0] if isinstance(selected, list) and selected else ""
        ).strip()
        if option_id not in valid:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _prompt_blocks(
    text: str,
    image_paths: list[str],
    *,
    supports_images: bool,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    attached: list[dict[str, Any]] = []
    path_fallbacks: list[str] = []
    total = 0
    for raw in image_paths:
        path = Path(raw)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if (
            not supports_images
            or size > _MAX_IMAGE_BYTES
            or total + size > _MAX_IMAGES_BYTES
        ):
            path_fallbacks.append(str(path.resolve()))
            continue
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            path_fallbacks.append(str(path.resolve()))
            continue
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            path_fallbacks.append(str(path.resolve()))
            continue
        total += size
        attached.append(
            {
                "type": "image",
                "data": data,
                "mimeType": mime,
                "uri": path.resolve().as_uri(),
            }
        )
    if path_fallbacks:
        listed = "\n".join(f"- {path}" for path in path_fallbacks)
        text += (
            "\n\nThe user attached image file(s). Open these local files with your "
            f"available filesystem tools before answering:\n{listed}"
        )
    blocks.append({"type": "text", "text": text})
    blocks.extend(attached)
    return blocks


class GrokBuildAdapter:
    id = "grok_build"
    label = "Grok Build"
    capabilities = CodingAgentCapabilities(
        terminal_agent=True,
        chat_api=False,
        a2a=True,
        mcp_inject=True,
        needs_api_key=False,
        needs_cli=True,
        resume=True,
    )

    def detect(self, settings: Any) -> CodingAgentInfo:
        cfg = coding_agent_cfg(settings, self.id)
        enabled = bool(cfg.get("enabled", True))
        override = str(cfg.get("cli_path") or "")
        path = which_cli("grok", override)
        default_args = str(cfg.get("default_args") or "")
        return CodingAgentInfo(
            id=self.id,
            label=self.label,
            enabled=enabled,
            available=bool(path) and enabled,
            status=f"Found: {path}" if path else _missing_status(),
            cli_path=path or override,
            default_args=default_args,
            capabilities=self.capabilities,
            models=[dict(row) for row in _CLI_MODELS],
        )

    def launch(
        self,
        *,
        prompt: str,
        system_prompt: str,
        cwd: str,
        conv_id: str,
        model: str,
        mcp_config_path: str,
        extra_args: str,
        cli_path: str,
        env: dict[str, str],
        push: Callable[[dict[str, Any]], None],
        session_id: str = "",
        run_id: str = "",
        cancel: threading.Event | None = None,
        timeout_s: float = 0.0,
        image_paths: list[str] | None = None,
    ) -> CodingAgentLaunchResult:
        selected_model = (model or "default").strip() or "default"
        binary = which_cli("grok", cli_path)
        if not binary:
            return CodingAgentLaunchResult(
                ok=False, error=_missing_status(), status="error"
            )

        child_env = dict(env or {})
        effort = str(child_env.pop(_EFFORT_ENV, "") or "").strip().lower()
        try:
            from backend.agent.secrets import get_key

            api_key = str(get_key("spacexai") or "").strip()
        except Exception:  # noqa: BLE001 - plugin may load before the host secrets service
            api_key = ""
        if api_key:
            child_env["XAI_API_KEY"] = api_key

        try:
            mcp_servers = load_mcp_servers(mcp_config_path)
            argv = build_grok_argv(
                binary=binary,
                model=selected_model,
                reasoning_effort=effort,
                extra_args=extra_args,
            )
        except (TypeError, ValueError) as exc:
            return CodingAgentLaunchResult(ok=False, error=str(exc), status="error")

        state = _AcpStream(conv_id, run_id, push)
        permissions = _PermissionBroker(conv_id, run_id, push)
        client = JsonRpcStdioClient(
            argv=argv,
            cwd=cwd,
            env_extra=child_env,
            notification_handler=state.on_notification,
            request_handler=permissions,
            cancel=cancel,
        )
        new_session_id = session_id
        turn_deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        proc = None
        registered = False

        def _result_error(exc: Exception, status: str) -> CodingAgentLaunchResult:
            nonlocal new_session_id
            message = str(exc).strip() or "Grok Build failed"
            if api_key:
                message = message.replace(api_key, "[redacted]")
            low = message.lower()
            if session_id and any(marker in low for marker in _STALE_SESSION_MARKERS):
                new_session_id = ""
            if any(marker in low for marker in _LOGIN_MARKERS):
                status = "needs_login"
                message += (
                    " Save a SpaceXAI key in Ducky or run `grok login` in PowerShell, "
                    "then try again."
                )
            state.finish_unresolved_tools(cancelled=status == "cancelled")
            blocks = state.finalize_blocks()
            streamed_all = "".join(state.text_parts).strip()
            reply = state.trailing_text() or ("" if blocks else streamed_all)
            stderr_tail = client.stderr_tail[-4000:]
            if api_key:
                stderr_tail = stderr_tail.replace(api_key, "[redacted]")
            return CodingAgentLaunchResult(
                ok=False,
                upstream_session_id=new_session_id,
                output_tail=stderr_tail,
                reply_text=reply,
                error=message,
                status=status,
                streamed=bool(streamed_all) or bool(blocks),
                blocks=blocks,
            )

        try:
            push(
                {
                    "type": "status",
                    "text": "Resuming Grok Build…"
                    if session_id
                    else "Starting Grok Build…",
                    "conv_id": conv_id,
                    "run_id": run_id,
                }
            )
            proc = client.start()
            try:
                from backend.agent.coding_agents.proc_exec import register_process

                register_process(conv_id, proc)
                registered = True
            except Exception:  # noqa: BLE001 - standalone tests/older hosts lack registry hooks
                registered = False

            initialize = client.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "UEFN-Ducky", "version": _PLUGIN_VERSION},
                    "_meta": {
                        "startupHints": {
                            "nonInteractive": True,
                            "skipGitStatus": True,
                            "skipProjectLayout": True,
                        },
                        "clientType": "uefn-ducky",
                        "clientVersion": _PLUGIN_VERSION,
                    },
                },
                deadline=_deadline(turn_deadline, _HANDSHAKE_TIMEOUT_S),
            )
            if initialize.get("protocolVersion") != ACP_PROTOCOL_VERSION:
                raise AcpProtocolError(
                    f"Installed Grok Build negotiated unsupported ACP version "
                    f"{initialize.get('protocolVersion')!r}"
                )
            capabilities = initialize.get("agentCapabilities")
            if not isinstance(capabilities, dict):
                capabilities = {}
            validate_mcp_capabilities(mcp_servers, capabilities)

            auth_methods = initialize.get("authMethods")
            method_ids = {
                str(item.get("id") or "")
                for item in auth_methods or []
                if isinstance(item, dict)
            }
            auth_method = ""
            if api_key and "xai.api_key" in method_ids:
                auth_method = "xai.api_key"
            elif "cached_token" in method_ids:
                auth_method = "cached_token"
            elif api_key:
                raise _NeedsLogin(
                    "Installed Grok Build did not offer xai.api_key authentication."
                )
            else:
                raise _NeedsLogin("Grok Build is not signed in.")
            client.request(
                "authenticate",
                {"methodId": auth_method, "_meta": {"headless": True}},
                deadline=_deadline(turn_deadline, _HANDSHAKE_TIMEOUT_S),
            )

            if session_id:
                session_caps = capabilities.get("sessionCapabilities")
                if not isinstance(session_caps, dict):
                    session_caps = {}
                if session_caps.get("resume") is not None:
                    client.request(
                        "session/resume",
                        {
                            "sessionId": session_id,
                            "cwd": str(Path(cwd).resolve()),
                            "mcpServers": mcp_servers,
                        },
                        deadline=_deadline(turn_deadline, _HANDSHAKE_TIMEOUT_S),
                    )
                elif capabilities.get("loadSession"):
                    client.request(
                        "session/load",
                        {
                            "sessionId": session_id,
                            "cwd": str(Path(cwd).resolve()),
                            "mcpServers": mcp_servers,
                        },
                        deadline=_deadline(turn_deadline, _HANDSHAKE_TIMEOUT_S),
                    )
                else:
                    raise AcpProtocolError(
                        "Installed Grok Build cannot resume ACP sessions"
                    )
            else:
                session_response = client.request(
                    "session/new",
                    {
                        "cwd": str(Path(cwd).resolve()),
                        "mcpServers": mcp_servers,
                        "_meta": {
                            "sessionKind": "headless",
                            "clientIdentifier": "uefn-ducky",
                        },
                    },
                    deadline=_deadline(turn_deadline, _HANDSHAKE_TIMEOUT_S),
                )
                new_session_id = str(session_response.get("sessionId") or "").strip()
                if not new_session_id:
                    raise AcpProtocolError(
                        "Grok Build session/new returned no sessionId"
                    )

            state.session_id = new_session_id
            state.active = True
            full_prompt = prompt
            if system_prompt.strip() and not session_id:
                full_prompt = (
                    "<ducky_system_instructions>\n"
                    + system_prompt.strip()
                    + "\n</ducky_system_instructions>\n\n<user_request>\n"
                    + prompt
                    + "\n</user_request>"
                )
            prompt_caps = capabilities.get("promptCapabilities")
            supports_images = isinstance(prompt_caps, dict) and bool(
                prompt_caps.get("image")
            )
            response = client.request(
                "session/prompt",
                {
                    "sessionId": new_session_id,
                    "prompt": _prompt_blocks(
                        full_prompt,
                        list(image_paths or []),
                        supports_images=supports_images,
                    ),
                },
                deadline=turn_deadline,
                cancel_session_id=new_session_id,
            )
            state.finish_unresolved_tools(cancelled=False)
            blocks = state.finalize_blocks()
            streamed_all = "".join(state.text_parts).strip()
            reply = state.trailing_text() or ("" if blocks else streamed_all)
            return CodingAgentLaunchResult(
                ok=True,
                upstream_session_id=new_session_id,
                reply_text=reply,
                output_tail="",
                status="done",
                streamed=bool(streamed_all) or bool(blocks),
                usage=_usage_from_prompt_response(response, selected_model),
                blocks=blocks,
            )
        except AcpCancelled as exc:
            return _result_error(exc, "cancelled")
        except AcpTimeout as exc:
            return _result_error(exc, "timeout")
        except (AcpRemoteError, AcpProtocolError, AcpError, OSError) as exc:
            return _result_error(exc, "error")
        finally:
            if registered and proc is not None:
                try:
                    from backend.agent.coding_agents.proc_exec import unregister_process

                    unregister_process(conv_id, proc)
                except Exception:  # noqa: BLE001 - cleanup must not mask the turn result
                    registered = False
            client.close()
