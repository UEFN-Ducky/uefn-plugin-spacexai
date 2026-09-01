"""Small, dependency-free ACP v1 client used by the Grok Build adapter.

Grok Build exposes an Agent Client Protocol server on ``grok agent stdio``.
This module owns only transport concerns: safe argv construction, Ducky MCP
config conversion, JSON-RPC framing, cancellation, and child cleanup.  The
adapter in :mod:`grok_build_adapter` maps ACP events onto Ducky's UI model.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

ACP_PROTOCOL_VERSION = 1
_STDERR_TAIL_CHARS = 8_000
_RAW_TAIL_LINES = 40
_CANCEL_GRACE_S = 3.0


class AcpError(RuntimeError):
    """Base class for local ACP transport failures."""


class AcpProtocolError(AcpError):
    """The subprocess emitted an invalid ACP/JSON-RPC message."""


class AcpRemoteError(AcpError):
    """The ACP agent returned a JSON-RPC error."""

    def __init__(
        self, message: str, *, code: int | None = None, data: Any = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AcpProcessError(AcpError):
    """The ACP child exited before completing the current request."""


class AcpCancelled(AcpError):
    """The caller cancelled the current operation."""


class AcpTimeout(AcpError):
    """The current ACP operation exceeded its deadline."""


class AcpMethodNotFound(AcpError):
    """An ACP reverse request is not implemented by this client."""


def _windows_taskkill_path() -> str:
    """Resolve the OS-owned taskkill binary without trusting ``PATH``."""
    if os.name != "nt":
        return ""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if 0 < length < len(buffer):
            candidate = Path(buffer.value) / "taskkill.exe"
            if candidate.is_file():
                return str(candidate)
    except (AttributeError, OSError, ValueError):
        pass
    return ""


def _split_extra_args(raw: str) -> list[str]:
    """Split a settings string without passing quote characters to Popen.

    ``shlex.split(..., posix=False)`` best matches Windows command-line
    grouping, but deliberately preserves wrapping quotes.  Remove only one
    matching outer pair; embedded quotes and backslashes remain untouched.
    """
    text = (raw or "").strip()
    if not text:
        return []
    parts = shlex.split(text, posix=os.name != "nt")
    out: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
            part = part[1:-1]
        out.append(part)
    return out


def _validated_extra_args(raw: str) -> list[str]:
    """Reject flags that could replace Ducky's ACP transport/session wiring."""
    parts = _split_extra_args(raw)
    forbidden_value_flags = {
        "-m",
        "--model",
        "--reasoning-effort",
        "--effort",
        "--output-format",
        "--session-id",
        "--resume",
        "-r",
        "--cwd",
    }
    forbidden_flags = {
        "--no-auto-update",
        "--reauth",
        "--reauthenticate",
        "--single",
        "-p",
        "--always-approve",
        "--approve-all",
        "--yes",
        "--yolo",
        "--dangerously-skip-permissions",
        "--allow-all",
    }
    forbidden_commands = {"agent", "stdio", "headless", "serve", "leader"}
    out: list[str] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        key = token.split("=", 1)[0]
        if key in forbidden_value_flags:
            raise ValueError(
                f"Grok Build arguments may not override {key}; Ducky owns it"
            )
        if key in forbidden_flags or token.lower() in forbidden_commands:
            raise ValueError(f"Grok Build argument {token!r} conflicts with ACP mode")
        out.append(token)
        i += 1
    return out


def build_grok_argv(
    *,
    binary: str,
    model: str,
    reasoning_effort: str,
    extra_args: str,
) -> list[str]:
    """Build ``grok agent stdio`` argv without putting prompts or keys in it."""
    executable = (binary or "").strip()
    if not executable:
        raise ValueError("Grok Build CLI path is empty")
    argv = [executable, "--no-auto-update", "agent"]
    argv.extend(_validated_extra_args(extra_args))

    selected_model = (model or "").strip()
    if selected_model and selected_model.lower() not in ("default", "auto"):
        argv.extend(["--model", selected_model])

    effort = (reasoning_effort or "").strip().lower()
    if effort:
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported Grok Build reasoning effort: {effort!r}")
        argv.extend(["--reasoning-effort", effort])

    argv.append("stdio")
    return argv


def _string_map(value: Any, *, field: str) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"MCP {field} must be an object")
    out: dict[str, str] = {}
    for key, item in value.items():
        name = str(key or "").strip()
        if not name:
            raise ValueError(f"MCP {field} contains an empty name")
        if not isinstance(item, (str, int, float, bool)):
            raise TypeError(f"MCP {field}.{name} must be a scalar value")
        out[name] = str(item)
    return out


def load_mcp_servers(path: str) -> list[dict[str, Any]]:
    """Convert Ducky/Claude-style ``mcpServers`` JSON to ACP v1 entries.

    ACP represents environment variables and headers as ``[{name, value}]``
    rather than maps.  No project or user config file is written.
    """
    raw_path = (path or "").strip()
    if not raw_path:
        return []
    source = Path(raw_path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read Ducky MCP config: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ducky MCP config is not valid JSON: {exc}") from exc
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        raise TypeError("Ducky MCP config must contain an mcpServers object")

    out: list[dict[str, Any]] = []
    for raw_name, raw_config in servers.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_config, dict):
            raise ValueError("Each MCP server needs a non-empty name and object config")
        transport = str(
            raw_config.get("type") or ("http" if raw_config.get("url") else "stdio")
        )
        transport = transport.strip().lower()
        if transport in {"http", "sse"}:
            url = str(raw_config.get("url") or "").strip()
            if not url:
                raise ValueError(f"MCP server {name!r} is missing its URL")
            headers = _string_map(raw_config.get("headers"), field=f"{name}.headers")
            out.append(
                {
                    "type": transport,
                    "name": name,
                    "url": url,
                    "headers": [
                        {"name": key, "value": value} for key, value in headers.items()
                    ],
                }
            )
            continue
        if transport != "stdio":
            raise ValueError(
                f"MCP server {name!r} has unsupported transport {transport!r}"
            )

        command = str(raw_config.get("command") or "").strip()
        if not command:
            raise ValueError(f"MCP server {name!r} is missing its command")
        if not Path(command).is_absolute():
            command = shutil.which(command) or command
        raw_args = raw_config.get("args") or []
        if not isinstance(raw_args, list) or not all(
            isinstance(arg, str) for arg in raw_args
        ):
            raise ValueError(f"MCP server {name!r} args must be a list of strings")
        env = _string_map(raw_config.get("env"), field=f"{name}.env")
        out.append(
            {
                "name": name,
                "command": command,
                "args": list(raw_args),
                "env": [{"name": key, "value": value} for key, value in env.items()],
            }
        )
    return out


def validate_mcp_capabilities(
    servers: list[dict[str, Any]], agent_capabilities: dict[str, Any]
) -> None:
    """Fail rather than silently dropping a host-injected MCP server."""
    mcp_caps = agent_capabilities.get("mcpCapabilities")
    if not isinstance(mcp_caps, dict):
        mcp_caps = {}
    for server in servers:
        transport = str(server.get("type") or "stdio")
        if transport in {"http", "sse"} and not bool(mcp_caps.get(transport)):
            raise AcpProtocolError(
                f"Installed Grok Build does not support ACP {transport.upper()} MCP servers"
            )


class _ReverseCall:
    def __init__(self, request_id: Any, method: str) -> None:
        self.request_id = request_id
        self.method = method
        self.done = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None
        self.cancelled = False


class JsonRpcStdioClient:
    """One-process, one-turn JSON-RPC 2.0 client for ACP over stdio."""

    def __init__(
        self,
        *,
        argv: list[str],
        cwd: str,
        env_extra: dict[str, str] | None = None,
        notification_handler: Callable[[str, dict[str, Any]], None] | None = None,
        request_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env_extra = dict(env_extra or {})
        self.notification_handler = notification_handler
        self.request_handler = request_handler
        self.cancel = cancel
        self.process: subprocess.Popen[str] | None = None
        self._incoming: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
        self._stderr: deque[str] = deque()
        self._stderr_chars = 0
        self._raw_tail: deque[str] = deque(maxlen=_RAW_TAIL_LINES)
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._reverse_calls: dict[Any, _ReverseCall] = {}

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-_STDERR_TAIL_CHARS:]

    @property
    def raw_tail(self) -> str:
        return "\n".join(self._raw_tail)

    def start(self) -> subprocess.Popen[str]:
        if self.process is not None:
            return self.process
        env = dict(os.environ)
        for key, value in self.env_extra.items():
            if key:
                env[str(key)] = str(value)
        workdir = self.cwd if self.cwd and os.path.isdir(self.cwd) else os.getcwd()
        kwargs: dict[str, Any] = {
            "cwd": workdir,
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            # argv is a list, shell=False is the default, and the executable was
            # resolved by the adapter's which_cli() boundary.
            self.process = subprocess.Popen(self.argv, **kwargs)
        except OSError as exc:
            raise AcpProcessError(f"Could not start Grok Build: {exc}") from exc

        threading.Thread(
            target=self._read_stdout,
            name="grok-acp-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            name="grok-acp-stderr",
            daemon=True,
        ).start()
        return self.process

    def _read_stdout(self) -> None:
        proc = self.process
        if proc is None:
            self._incoming.put(AcpProcessError("Grok Build process was not started"))
            self._incoming.put(None)
            return
        try:
            for raw in proc.stdout or ():
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                self._raw_tail.append(line)
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._incoming.put(
                        AcpProtocolError(f"Grok Build emitted invalid JSON: {exc}")
                    )
                    continue
                if not isinstance(message, dict):
                    self._incoming.put(
                        AcpProtocolError(
                            "Grok Build emitted a non-object JSON-RPC message"
                        )
                    )
                    continue
                self._incoming.put(message)
        except (OSError, ValueError) as exc:
            self._incoming.put(AcpProcessError(f"Lost Grok Build stdout: {exc}"))
        finally:
            self._incoming.put(None)

    def _read_stderr(self) -> None:
        proc = self.process
        if proc is None:
            return
        try:
            for text in proc.stderr or ():
                self._stderr.append(text)
                self._stderr_chars += len(text)
                while self._stderr and self._stderr_chars > _STDERR_TAIL_CHARS * 2:
                    self._stderr_chars -= len(self._stderr.popleft())
        except (OSError, ValueError):
            pass

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AcpProcessError("Grok Build stdin is unavailable")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._write_lock:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise AcpProcessError("Grok Build closed its ACP input") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _begin_reverse_call(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if self.request_handler is None:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
            return

        call = _ReverseCall(request_id, method)
        self._reverse_calls[request_id] = call

        def _run() -> None:
            try:
                call.result = self.request_handler(method, params)
            except Exception as exc:  # noqa: BLE001 - callback boundary becomes JSON-RPC error
                call.error = exc
            finally:
                call.done.set()

        threading.Thread(target=_run, name="grok-acp-reverse", daemon=True).start()

    def _flush_reverse_calls(self, *, cancel_pending: bool = False) -> None:
        for request_id, call in list(self._reverse_calls.items()):
            if cancel_pending and not call.done.is_set():
                call.cancelled = True
                call.result = {"outcome": {"outcome": "cancelled"}}
                call.done.set()
            if not call.done.is_set():
                continue
            self._reverse_calls.pop(request_id, None)
            if call.cancelled:
                result = {"outcome": {"outcome": "cancelled"}}
                self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
            elif isinstance(call.error, AcpMethodNotFound):
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": str(call.error)},
                    }
                )
            elif call.error is not None:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "Client request failed"},
                    }
                )
            else:
                self._send(
                    {"jsonrpc": "2.0", "id": request_id, "result": call.result or {}}
                )

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        deadline: float | None = None,
        cancel_session_id: str = "",
    ) -> dict[str, Any]:
        """Send one request and pump notifications/reverse calls until it resolves."""
        self.start()
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        cancelling = False
        timed_out = False
        cancel_deadline: float | None = None

        while True:
            now = time.monotonic()
            should_cancel = self.cancel is not None and self.cancel.is_set()
            should_timeout = deadline is not None and now >= deadline
            if not cancelling and (should_cancel or should_timeout):
                cancelling = True
                timed_out = bool(should_timeout and not should_cancel)
                cancel_deadline = now + _CANCEL_GRACE_S
                self._flush_reverse_calls(cancel_pending=True)
                if cancel_session_id:
                    self.notify(
                        "session/cancel",
                        {
                            "sessionId": cancel_session_id,
                            "_meta": {
                                "cancelTrigger": "timeout" if timed_out else "user"
                            },
                        },
                    )
                else:
                    raise (
                        AcpTimeout(f"ACP {method} timed out")
                        if timed_out
                        else AcpCancelled("Cancelled")
                    )

            self._flush_reverse_calls(cancel_pending=cancelling)
            if cancelling and cancel_deadline is not None and now >= cancel_deadline:
                if timed_out:
                    raise AcpTimeout(f"ACP {method} timed out")
                raise AcpCancelled("Cancelled")

            try:
                incoming = self._incoming.get(timeout=0.05)
            except queue.Empty:
                if self.process is not None and self.process.poll() is not None:
                    detail = self.stderr_tail.strip()
                    raise AcpProcessError(
                        detail or f"Grok Build exited with {self.process.returncode}"
                    )
                continue
            if incoming is None:
                detail = self.stderr_tail.strip()
                raise AcpProcessError(detail or "Grok Build closed its ACP output")
            if isinstance(incoming, Exception):
                raise incoming

            if "method" in incoming:
                incoming_method = str(incoming.get("method") or "")
                if "id" in incoming:
                    self._begin_reverse_call(incoming)
                else:
                    incoming_params = incoming.get("params")
                    if not isinstance(incoming_params, dict):
                        incoming_params = {}
                    if self.notification_handler is not None:
                        self.notification_handler(incoming_method, incoming_params)
                continue
            if incoming.get("id") != request_id:
                continue
            error = incoming.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "ACP request failed")
                data = error.get("data")
                if data not in (None, ""):
                    detail = (
                        data
                        if isinstance(data, str)
                        else json.dumps(data, ensure_ascii=False)
                    )
                    message = f"{message}: {detail}"
                code = error.get("code")
                raise AcpRemoteError(
                    message,
                    code=int(code) if isinstance(code, int) else None,
                    data=data,
                )
            result = incoming.get("result")
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise AcpProtocolError(f"ACP {method} returned a non-object result")
            if cancelling:
                if timed_out:
                    raise AcpTimeout(f"ACP {method} timed out")
                raise AcpCancelled("Cancelled")
            return result

    def close(self) -> None:
        """Close stdin first, then reap the process and its child tree."""
        proc = self.process
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
        try:
            proc.wait(timeout=3.0)
        except (OSError, subprocess.TimeoutExpired):
            self._kill_tree(proc)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            taskkill = _windows_taskkill_path()
            if taskkill:
                try:
                    subprocess.run(
                        [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=5,
                    )
                    return
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
