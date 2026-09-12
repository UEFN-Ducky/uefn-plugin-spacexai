from __future__ import annotations

from backend.spacexai_provider import grok_reasoning_effort, grok_supports_reasoning_effort


def test_grok_reasoning_effort() -> None:
    assert grok_supports_reasoning_effort("grok-4.6")
    assert grok_supports_reasoning_effort("grok-4.3")
    assert not grok_supports_reasoning_effort("grok-4")
    assert not grok_supports_reasoning_effort("grok-3")
    assert grok_reasoning_effort("grok-4.3", "off") == "none"
    assert grok_reasoning_effort("grok-4.5", "off") is None
    assert grok_reasoning_effort("grok-4.6", "high") == "high"
    assert grok_reasoning_effort("grok-4", "high") is None


if __name__ == "__main__":
    test_grok_reasoning_effort()
    print("ok")
