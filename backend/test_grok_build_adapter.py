from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import backend.grok_build_adapter as adapter_module
from backend.agent import secrets
from backend.grok_build_adapter import GrokBuildAdapter, _prompt_blocks

_FAKE_AGENT = r"""from __future__ import annotations
import json
import os
import sys
import time

mode = os.environ.get("FAKE_ACP_MODE", "success")
log_path = os.environ["FAKE_ACP_LOG"]

def record(value):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")

def send(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)

def result(request_id, value):
    send({"jsonrpc": "2.0", "id": request_id, "result": value})

def notification(session_id, update):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    })

for raw in sys.stdin:
    message = json.loads(raw)
    if "method" in message:
        record({
            "method": message.get("method"),
            "params": message.get("params"),
            "seen_key": bool(os.environ.get("XAI_API_KEY")),
        })
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        auth = [{"id": "grok.com", "name": "Grok.com"}]
        if mode != "noauth":
            auth = [
                {"id": "xai.api_key", "name": "xAI API key"},
                {"id": "cached_token", "name": "Saved login"},
                {"id": "grok.com", "name": "grok.com"},
            ]
        result(request_id, {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": mode == "images"},
                "mcpCapabilities": {"http": True, "sse": True},
                "sessionCapabilities": {"resume": {}},
            },
            "authMethods": auth,
        })
    elif method == "authenticate":
        result(request_id, {})
    elif method == "session/new":
        result(request_id, {"sessionId": "session-new"})
    elif method == "session/resume":
        if mode == "stale":
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "session not found while resuming"},
            })
        else:
            result(request_id, {})
    elif method == "session/load":
        result(request_id, {})
    elif method == "session/prompt":
        session_id = message["params"]["sessionId"]
        if mode == "malformed":
            print("not-json", flush=True)
            time.sleep(5)
            continue
        if mode == "cancel":
            for nested_raw in sys.stdin:
                nested = json.loads(nested_raw)
                if nested.get("method") == "session/cancel":
                    record({"method": "session/cancel", "params": nested.get("params")})
                    result(request_id, {"stopReason": "cancelled"})
                    break
            continue

        notification(session_id, {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "Checking the project."},
        })
        notification(session_id, {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "I found the issue. "},
        })
        notification(session_id, {
            "sessionUpdate": "tool_call",
            "toolCallId": "tool-1",
            "title": "uefn/inspect_project",
            "kind": "read",
            "status": "pending",
            "rawInput": {"project": "Island"},
        })
        send({
            "jsonrpc": "2.0",
            "id": 900,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "tool-1",
                    "title": "uefn/inspect_project",
                    "kind": "read",
                    "rawInput": {"project": "Island"},
                },
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        })
        permission = json.loads(next(sys.stdin))
        record({"permission_response": permission})
        notification(session_id, {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "Project ready"}}
            ],
            "rawOutput": {"ok": True},
        })
        notification(session_id, {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Done."},
        })
        result(request_id, {
            "stopReason": "end_turn",
            "_meta": {
                "modelId": "grok-4.6",
                "usage": {
                    "inputTokens": 120,
                    "outputTokens": 20,
                    "cachedReadTokens": 50,
                    "cacheCreationTokens": 10,
                    "numTurns": 2,
                    "costUsdTicks": 100000000,
                    "modelUsage": {"grok-4.6": {"inputTokens": 120}},
                },
            },
        })
"""


def _mcp_config(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "uefn": {
                        "type": "stdio",
                        "command": "ducky-bridge",
                        "args": ["--stdio"],
                        "env": {"DUCKY_CONV_ID": "chat-1"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _configure_fake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "success",
    api_key: str = "test-secret-key",
) -> tuple[Path, dict[str, str]]:
    script = tmp_path / "fake_grok_acp.py"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    log = tmp_path / "acp-log.jsonl"
    monkeypatch.setattr(
        adapter_module,
        "build_grok_argv",
        lambda **kwargs: [sys.executable, "-u", str(script)],
    )
    monkeypatch.setattr(secrets, "get_key", lambda name: api_key)
    return log, {"FAKE_ACP_MODE": mode, "FAKE_ACP_LOG": str(log)}


def _launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "success",
    api_key: str = "test-secret-key",
    session_id: str = "",
    cancel: threading.Event | None = None,
    image_paths: list[str] | None = None,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]], str]:
    log_path, fake_env = _configure_fake(
        monkeypatch, tmp_path, mode=mode, api_key=api_key
    )
    import backend.panel.rpc as panel_rpc_module

    monkeypatch.setattr(
        panel_rpc_module,
        "panel_rpc",
        lambda method, params, timeout=30.0: {
            "ok": True,
            "answers": {"grok_permission": {"selected": ["allow-once"]}},
        },
    )
    events: list[dict[str, Any]] = []
    result = GrokBuildAdapter().launch(
        prompt="Fix the Verse device.",
        system_prompt="Follow Ducky's project rules.",
        cwd=str(tmp_path),
        conv_id="chat-1",
        model="default",
        mcp_config_path=str(_mcp_config(tmp_path)),
        extra_args="",
        cli_path=sys.executable,
        env={**fake_env, "DUCKY_GROK_REASONING_EFFORT": "high"},
        push=events.append,
        session_id=session_id,
        run_id="run-1",
        cancel=cancel,
        timeout_s=5,
        image_paths=image_paths,
    )
    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    return result, events, records, log_path.read_text(encoding="utf-8")


def _request(records: list[dict[str, Any]], method: str) -> dict[str, Any]:
    return next(row for row in records if row.get("method") == method)


def test_launch_runs_full_acp_turn_with_mcp_permission_and_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, events, records, raw_log = _launch(monkeypatch, tmp_path)

    assert result.ok is True
    assert result.status == "done"
    assert result.upstream_session_id == "session-new"
    assert result.reply_text == "Done."
    assert result.usage == {
        "input_tokens": 60,
        "output_tokens": 20,
        "cache_read_tokens": 50,
        "cache_write_tokens": 10,
        "context_tokens": 120,
        "cost_usd": 0.01,
        "num_turns": 2,
        "model": "grok-4.6",
    }
    assert [event["type"] for event in events if event["type"] != "status"] == [
        "thinking",
        "text_delta",
        "tool",
        "tool_done",
        "text_delta",
    ]
    assert [block["type"] for block in result.blocks] == [
        "thinking",
        "text",
        "tool_call",
    ]
    assert result.blocks[-1]["result"]["data"] == "Project ready"

    assert _request(records, "authenticate")["params"]["methodId"] == "xai.api_key"
    new_session = _request(records, "session/new")["params"]
    assert new_session["_meta"]["sessionKind"] == "headless"
    assert new_session["mcpServers"][0]["name"] == "uefn"
    assert new_session["mcpServers"][0]["env"] == [
        {"name": "DUCKY_CONV_ID", "value": "chat-1"}
    ]
    prompt = _request(records, "session/prompt")["params"]["prompt"][0]["text"]
    assert "<ducky_system_instructions>" in prompt
    assert "Fix the Verse device." in prompt
    permission = next(
        row["permission_response"] for row in records if "permission_response" in row
    )
    assert permission["result"] == {
        "outcome": {"outcome": "selected", "optionId": "allow-once"}
    }
    assert "test-secret-key" not in raw_log
    assert _request(records, "initialize")["seen_key"] is True


def test_resume_uses_cached_login_and_does_not_repeat_system_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _events, records, _raw_log = _launch(
        monkeypatch, tmp_path, api_key="", session_id="session-old"
    )
    assert result.ok is True
    assert result.upstream_session_id == "session-old"
    assert _request(records, "authenticate")["params"]["methodId"] == "cached_token"
    assert _request(records, "session/resume")["params"]["sessionId"] == "session-old"
    prompt = _request(records, "session/prompt")["params"]["prompt"][0]["text"]
    assert "Follow Ducky's project rules." not in prompt
    assert prompt == "Fix the Verse device."


def test_missing_login_returns_needs_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _events, records, _raw_log = _launch(
        monkeypatch, tmp_path, mode="noauth", api_key=""
    )
    assert result.ok is False
    assert result.status == "needs_login"
    assert "grok login" in result.error
    assert not any(row.get("method") == "authenticate" for row in records)


def test_stale_resume_id_is_cleared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _events, _records, _raw_log = _launch(
        monkeypatch, tmp_path, mode="stale", session_id="missing-session"
    )
    assert result.ok is False
    assert result.upstream_session_id == ""
    assert "session not found" in result.error


def test_user_cancel_sends_session_cancel_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        result, _events, records, _raw_log = _launch(
            monkeypatch, tmp_path, mode="cancel", cancel=cancel
        )
    finally:
        timer.cancel()
    assert result.ok is False
    assert result.status == "cancelled"
    assert _request(records, "session/cancel")["params"]["sessionId"] == "session-new"


def test_malformed_agent_output_is_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _events, _records, _raw_log = _launch(
        monkeypatch, tmp_path, mode="malformed"
    )
    assert result.ok is False
    assert result.status == "error"
    assert "invalid JSON" in result.error


def test_prompt_blocks_embed_supported_images_and_fall_back_to_paths(
    tmp_path: Path,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    embedded = _prompt_blocks("Look", [str(image)], supports_images=True)
    assert embedded[1]["type"] == "image"
    assert embedded[1]["mimeType"] == "image/png"
    fallback = _prompt_blocks("Look", [str(image)], supports_images=False)
    assert len(fallback) == 1
    assert str(image.resolve()) in fallback[0]["text"]
