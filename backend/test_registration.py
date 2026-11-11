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
    assert registration["resolve_api_fallback"]("default") is None
    assert registration["thinking_env"]("high") == {
        "DUCKY_GROK_REASONING_EFFORT": "high"
    }
    assert any("Grok Build" in message for message in api.logs)


def test_account_hooks_are_registered() -> None:
    api = _Api()
    register(api)
    agent = api.agents["grok_build"]
    for hook in ("account_env", "account_login", "account_status", "account_logout", "is_limit_error"):
        assert callable(agent[hook]), hook
    assert api.providers["spacexai"]["is_quota_error"]("429 Too Many Requests")


def test_login_account_is_its_own_grok_home(tmp_path) -> None:
    from backend.grok_build_adapter import account_env, account_logout, account_status

    home = tmp_path / "acc2"
    assert account_env(str(home)) == {"GROK_HOME": str(home)}
    assert account_status(str(home)) == {"loggedIn": False}
    (home / "auth.json").write_text('{"email": "a@x.ai"}', encoding="utf-8")
    assert account_status(str(home)) == {"loggedIn": True, "email": "a@x.ai"}
    account_logout(str(home))
    assert account_status(str(home))["loggedIn"] is False


class _FakeLogin:
    """`grok login --oauth` that signs in (writes auth.json) or gives up, after a moment."""

    def __init__(self, home, code=0, out=""):
        import time

        self.home, self.code, self.returncode = home, code, None
        self.stdout = iter([out]) if out else None
        self.started = time.monotonic()

    def poll(self):
        import time

        if self.returncode is None and time.monotonic() - self.started > 0.2:
            if self.code == 0:
                (self.home / "auth.json").write_text('{"email": "a@x.ai"}', encoding="utf-8")
            self.returncode = self.code
        return self.returncode

    def kill(self):
        self.returncode = -9


def _until_done(status, home):
    import time

    for _ in range(100):
        if not status(str(home)).get("pending"):
            return
        time.sleep(0.05)


def test_one_click_login_runs_hidden_grok_oauth_and_reads_signed_in(monkeypatch, tmp_path) -> None:
    from backend import grok_login
    from backend.grok_build_adapter import account_browser_login, account_status

    home = tmp_path / "acc3"
    seen = []
    monkeypatch.setattr(grok_login, "grok_bin", lambda override="": "grok")
    out = grok_login.start_browser_login(
        str(home), {"GROK_HOME": str(home)}, poll_s=0.02,
        popen=lambda argv, **kw: seen.append((argv, kw["env"].get("GROK_HOME"))) or _FakeLogin(home))
    assert out == {"message": grok_login.WAITING}
    assert account_status(str(home)) == {"loggedIn": False, "pending": True}
    _until_done(account_status, home)
    assert account_status(str(home)) == {"loggedIn": True, "email": "a@x.ai"}
    assert seen == [(["grok", "login", "--oauth"], str(home))]  # browser login, not device code
    assert callable(account_browser_login)


def test_one_click_login_cancelled_says_why(monkeypatch, tmp_path) -> None:
    from backend import grok_login
    from backend.grok_build_adapter import account_status

    home = tmp_path / "acc4"
    monkeypatch.setattr(grok_login, "grok_bin", lambda override="": "grok")
    grok_login.start_browser_login(str(home), {}, poll_s=0.02,
                                   popen=lambda *_a, **_k: _FakeLogin(home, 1, "Error: access denied\n"))
    _until_done(account_status, home)
    st = account_status(str(home))
    assert st["loggedIn"] is False and st["error"].startswith(grok_login.NOT_FINISHED)
    assert "access denied" in st["error"]


def test_missing_grok_installs_then_finds_it_without_a_restart(monkeypatch, tmp_path) -> None:
    import os
    import subprocess

    from backend import grok_login
    from backend.grok_build_adapter import account_status

    monkeypatch.setattr(grok_login.Path, "home", lambda: tmp_path)
    monkeypatch.setitem(grok_login._INSTALL, "busy", False)
    monkeypatch.setitem(grok_login._INSTALL, "error", "")
    import backend.agent.coding_agents.base as base

    monkeypatch.setattr(base, "which_cli", lambda name, override="": "")  # not on this process's PATH
    started = []
    monkeypatch.setattr(grok_login.threading, "Thread", lambda target, **_k: type(
        "T", (), {"start": lambda self: started.append(target)})())
    assert grok_login.grok_bin() == ""
    assert grok_login.start_browser_login(None, {}) == {"error": grok_login.INSTALLING}
    assert account_status(None) == {"loggedIn": False, "pending": True, "installing": True}

    def fake_install(argv, **_kw):  # the official installer puts grok in ~/.grok/bin
        exe = tmp_path / ".grok" / "bin" / ("grok.exe" if os.name == "nt" else "grok")
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setattr(grok_login.subprocess, "run", fake_install)
    started[0]()
    assert grok_login._INSTALL == {"busy": False, "error": ""}
    assert grok_login.grok_bin().endswith("grok.exe" if os.name == "nt" else "grok")


def test_default_account_is_grok_own_home(monkeypatch, tmp_path) -> None:
    from backend import grok_login
    from backend.grok_build_adapter import account_env

    assert account_env(None) == {}
    monkeypatch.setattr(grok_login.Path, "home", lambda: tmp_path)
    assert grok_login.grok_home(None) == tmp_path / ".grok"


def test_key_b_is_used_after_key_a_hits_429(monkeypatch) -> None:
    import sys
    import types

    import backend.agent as agent_pkg
    from backend import grok_build_adapter as gba

    exhausted: set[str] = set()
    fake = types.SimpleNamespace(
        pick_account=lambda p: "b" if "a" in exhausted else "a",
        account_key=lambda p, a: f"KEY-{a}",
        mark_exhausted=lambda p, a, until=None: exhausted.add(a),
    )
    monkeypatch.setattr(agent_pkg, "accounts", fake, raising=False)
    monkeypatch.setitem(sys.modules, "backend.agent.accounts", fake)

    assert gba.pick_xai_key({}) == ("a", "KEY-a")
    gba.note_key_limit("a", "Error: 429 Too Many Requests")
    assert gba.pick_xai_key({}) == ("b", "KEY-b")
    assert gba.pick_xai_key({"GROK_HOME": "x"}) == ("", "")
    assert not gba.is_limit_error("Context window full")
