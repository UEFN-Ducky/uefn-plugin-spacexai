from backend.agent.providers.base import ProviderMessage, ToolCallRequest
from backend.spacexai_provider import SpaceXAIProvider


def test_followup_comes_after_previous_reply():
    msgs = [
        ProviderMessage(role="user", content="first"),
        ProviderMessage(
            role="assistant",
            content="Checking",
            tool_calls=[ToolCallRequest(id="t1", name="ping", arguments={})],
        ),
        ProviderMessage(role="tool", tool_call_id="t1", content='{"ok":true}'),
        ProviderMessage(role="assistant", content="Here is the answer."),
        ProviderMessage(role="user", content="follow up"),
    ]
    out = SpaceXAIProvider("k", "grok-3")._to_openai_messages("sys", msgs)
    roles = [m["role"] for m in out if m["role"] != "system"]
    assert roles == ["user", "assistant", "tool", "assistant", "user"]
    assert out[-2]["content"] == "Here is the answer."
    assert out[-1]["content"] == "follow up"
