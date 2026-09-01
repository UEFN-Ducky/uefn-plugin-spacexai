from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.grok_acp import (
    AcpProtocolError,
    build_grok_argv,
    load_mcp_servers,
    validate_mcp_capabilities,
)


def test_build_grok_argv_owns_transport_model_and_effort() -> None:
    argv = build_grok_argv(
        binary="grok",
        model="grok-4.6",
        reasoning_effort="high",
        extra_args='--agent-profile "C:/Profiles/UEFN Agent.toml"',
    )
    assert argv[:3] == ["grok", "--no-auto-update", "agent"]
    assert argv[-1] == "stdio"
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "grok-4.6"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    assert "C:/Profiles/UEFN Agent.toml" in argv
    assert not any(token.startswith(('"', "'")) for token in argv)


def test_build_grok_argv_uses_installed_cli_default() -> None:
    argv = build_grok_argv(
        binary="grok", model="default", reasoning_effort="", extra_args=""
    )
    assert argv == ["grok", "--no-auto-update", "agent", "stdio"]


@pytest.mark.parametrize(
    "extra",
    [
        "--model other",
        "--reasoning-effort low",
        "--resume abc",
        "stdio",
        "--reauth",
        "--always-approve",
        "--approve-all",
        "--yes",
        "--yolo",
        "--dangerously-skip-permissions",
        "--allow-all",
    ],
)
def test_build_grok_argv_rejects_session_transport_overrides(extra: str) -> None:
    with pytest.raises(ValueError):
        build_grok_argv(
            binary="grok",
            model="default",
            reasoning_effort="medium",
            extra_args=extra,
        )


def test_load_mcp_servers_converts_stdio_and_http(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "uefn": {
                        "type": "stdio",
                        "command": "ducky-bridge",
                        "args": ["--stdio"],
                        "env": {"DUCKY_CONV_ID": "chat-1", "PORT": 1234},
                    },
                    "docs": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "headers": {"Authorization": "Bearer test"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    servers = load_mcp_servers(str(config))
    assert servers[0] == {
        "name": "uefn",
        "command": "ducky-bridge",
        "args": ["--stdio"],
        "env": [
            {"name": "DUCKY_CONV_ID", "value": "chat-1"},
            {"name": "PORT", "value": "1234"},
        ],
    }
    assert servers[1]["type"] == "http"
    assert servers[1]["headers"] == [{"name": "Authorization", "value": "Bearer test"}]


def test_remote_mcp_capability_fails_closed() -> None:
    servers = [{"type": "http", "name": "remote", "url": "https://example.test"}]
    with pytest.raises(AcpProtocolError):
        validate_mcp_capabilities(servers, {"mcpCapabilities": {"http": False}})
    validate_mcp_capabilities(servers, {"mcpCapabilities": {"http": True}})
