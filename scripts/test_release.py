from __future__ import annotations

import pytest

from scripts.release import _validated_base_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://uefnducky.org/", "https://uefnducky.org"),
        ("http://127.0.0.1:8787", "http://127.0.0.1:8787"),
        ("http://[::1]:8787/", "http://[::1]:8787"),
    ],
)
def test_store_base_url_accepts_https_and_loopback(value: str, expected: str) -> None:
    assert _validated_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "file:///tmp/plugin.zip",
        "https://token@example.com",
        "https://uefnducky.org?redirect=https://example.com",
    ],
)
def test_store_base_url_rejects_unsafe_destinations(value: str) -> None:
    with pytest.raises(SystemExit):
        _validated_base_url(value)
