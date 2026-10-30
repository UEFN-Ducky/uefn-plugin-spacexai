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
