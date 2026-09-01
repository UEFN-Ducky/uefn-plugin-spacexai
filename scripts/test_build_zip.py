from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.build_zip import build_zip


def _archive_names(tmp_path: Path) -> list[str]:
    destination = tmp_path / "spacexai-test.zip"
    build_zip(out=destination)
    with zipfile.ZipFile(destination) as archive:
        return archive.namelist()


def test_runtime_payload_is_complete(tmp_path: Path) -> None:
    names = _archive_names(tmp_path)

    assert names == [
        "LICENSE",
        "assets/icon.svg",
        "backend/__init__.py",
        "backend/grok_acp.py",
        "backend/grok_build_adapter.py",
        "backend/model_fetch.py",
        "backend/spacexai_provider.py",
        "plugin.json",
    ]


def test_development_files_are_not_shipped(tmp_path: Path) -> None:
    names = _archive_names(tmp_path)

    assert not any("test_" in name or "conftest" in name for name in names)
    assert not any("__pycache__" in name or name.startswith(".") for name in names)
