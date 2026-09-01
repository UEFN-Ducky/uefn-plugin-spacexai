"""Zip plugin.json + backend (+ assets) for Store upload. No secrets."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "deploy"
PACK_ROOTS = {"plugin.json", "LICENSE", "backend", "assets"}
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".zip", ".ducky-plugin"}
SKIP_FILES = {"conftest.py"}


def build_zip(*, out: Path | None = None) -> Path:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    pid = str(manifest.get("id") or "").strip()
    version = manifest.get("version") or 1
    if not pid:
        raise SystemExit("plugin.json missing id")
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".dat", ".env", ".pem", ".key"}:
            raise SystemExit(f"refusing to pack secret-looking file: {path}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = out or (OUT_DIR / f"{pid}-{version}.ducky-plugin.zip")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(ROOT).parts
            if not rel_parts or rel_parts[0] not in PACK_ROOTS:
                continue
            if any(part.startswith(".") or part in SKIP_PARTS for part in rel_parts):
                continue
            if (
                path.suffix.lower() in SKIP_SUFFIX
                or path.name.startswith(".")
                or path.name.startswith("test_")
                or path.name in SKIP_FILES
            ):
                continue
            zf.write(path, arcname="/".join(rel_parts))
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")
    return dest


if __name__ == "__main__":
    build_zip()
