"""Zip + publish SpaceXAI gateway to the UEFN Ducky Store via uds_release."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from build_zip import build_zip


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the Store bearer token to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _validated_base_url(value: str) -> str:
    """Allow HTTPS Store endpoints and explicit loopback development servers."""
    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit(
            "Store base URL must not contain credentials, a query, or a fragment"
        )
    if not parsed.netloc or not (
        parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback)
    ):
        raise SystemExit(
            "Store base URL must use HTTPS (HTTP is allowed only on loopback)"
        )
    return raw


def _load_dotenv() -> None:
    for path in (
        ROOT / ".env",
        ROOT.parents[1] / ".env",
        ROOT.parents[1].parent / "DuckyOS" / ".env",
        Path.home() / ".duckyos" / ".env",
    ):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def mcp_call(base_url: str, api_key: str, name: str, arguments: dict) -> dict:
    base_url = _validated_base_url(base_url)
    url = base_url + "/api/v1/mcp"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "UEFN-Ducky-PluginRelease/1.0",
        },
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"MCP HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"MCP request failed: {exc.reason}") from exc
    if payload.get("error"):
        raise SystemExit(f"MCP error: {payload['error']}")
    result = payload.get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text.startswith("{"):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
            return {"ok": True, "text": text}
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def _api_key() -> str:
    key = (
        os.environ.get("DUCKYOS_API_KEY")
        or os.environ.get("DUCKYOS_MARKETPLACE_API_KEY")
        or ""
    )
    if key:
        return key
    try:
        data = json.loads(
            (Path.home() / ".cursor" / "mcp.json").read_text(encoding="utf-8")
        )
        headers = ((data.get("mcpServers") or {}).get("uefn-duckyos-site") or {}).get(
            "headers"
        ) or {}
        auth = str(headers.get("Authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
    except (AttributeError, json.JSONDecodeError, OSError, TypeError):
        return ""
    return ""


def upload_zip_file(base_url: str, api_key: str, zip_path: Path) -> dict:
    """Upload the archive outside MCP JSON's request-size limit."""
    base_url = _validated_base_url(base_url)
    boundary = "----DuckyPluginZipBoundary" + secrets.token_hex(16)
    filename = zip_path.name
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/zip\r\n\r\n"
            ).encode(),
            zip_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(
        base_url + "/api/files/app-release",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "UEFN-Ducky-PluginRelease/1.0",
            "Origin": base_url,
        },
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Upload HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Upload failed: {exc.reason}") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise SystemExit(f"Upload failed: {payload}")
    return payload


def publish(zip_path: Path, *, category: str, changelog: str) -> None:
    _load_dotenv()
    base = _validated_base_url(
        os.environ.get("DUCKYOS_BASE_URL")
        or os.environ.get("DUCKYOS_MARKETPLACE_URL")
        or "https://uefnducky.org"
    )
    key = _api_key()
    if not key:
        raise SystemExit("Set DUCKYOS_API_KEY to publish")

    mcp_call(base, key, "uds_ensure_category", {"name": "Gateways", "slug": "gateways"})
    mcp_call(
        base, key, "uds_ensure_category", {"name": category.title(), "slug": category}
    )
    uploaded = upload_zip_file(base, key, zip_path)
    file_meta = uploaded.get("file") if isinstance(uploaded.get("file"), dict) else {}
    file_id = str(file_meta.get("id") or uploaded.get("id") or "").strip()
    zip_url = str(uploaded.get("url") or "").strip()
    if not file_id and not zip_url:
        raise SystemExit(f"Upload response missing a file id or URL: {uploaded}")
    release_args = {
        "category": category,
        "changelog": changelog or "v1.1.0: Add Grok Build coding agent over ACP",
        "publish": True,
        "categories": ["plugins", "gateways"],
        "tags": ["spacexai", "xai", "grok", "grok-build", "acp", "llm", "coding-agent"],
        "name": "SpaceXAI",
        "description": (
            "SpaceXAI's Grok API provider plus the official Grok Build coding agent. "
            "Grok Build supports Ducky's UEFN MCP tools, streaming, permissions, and session resume."
        ),
    }
    if file_id:
        release_args["zipFileId"] = file_id
    else:
        release_args["zipUrl"] = zip_url
    result = mcp_call(
        base,
        key,
        "uds_release",
        release_args,
    )
    print(json.dumps(result, indent=2))
    if result.get("ok") is False or result.get("error"):
        raise SystemExit(result.get("error") or "release failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--changelog", default="")
    parser.add_argument(
        "--category", default=os.environ.get("UDS_CATEGORY") or "plugins"
    )
    args = parser.parse_args()
    zip_path = build_zip()
    if args.publish:
        publish(zip_path, category=args.category, changelog=args.changelog)
    else:
        print("zip only — pass --publish to upload/approve on the Store")


if __name__ == "__main__":
    main()
