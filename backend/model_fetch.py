"""SpaceXAI / xAI model list fetch for this gateway plugin."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import fields
from typing import Any

from backend.agent.model_fetch import ModelInfo, _cache_put

from .spacexai_provider import SPACEXAI_BASE_URL

_log = logging.getLogger(__name__)
_CACHE_MAX = 512
_CACHE_TTL_S = 6 * 3600.0
_MODEL_INFO_FIELDS = {f.name for f in fields(ModelInfo)}


def _model_info(**kw: Any) -> ModelInfo:
    """Drop unknown fields so an older host ModelInfo does not TypeError."""
    return ModelInfo(**{k: v for k, v in kw.items() if k in _MODEL_INFO_FIELDS})

_SPACEXAI_MODELS_CACHE: dict[str, tuple[float, list[ModelInfo]]] = {}


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()


def clear_model_cache() -> None:
    _SPACEXAI_MODELS_CACHE.clear()


def fetch_models(api_key: str, **_kw: Any) -> list[ModelInfo]:
    return _fetch_spacexai(api_key)


def _media_fields(record: dict[str, Any] | None) -> dict[str, Any]:
    """max_images / video / audio from the API record; None when it does not say."""
    rec = record or {}
    mods = rec.get("input_modalities")
    known = isinstance(mods, list) and bool(mods)
    lowered = [str(m).lower() for m in mods] if known else []
    max_images = rec.get("max_images")
    return {
        "max_images": max_images if isinstance(max_images, int) and not isinstance(max_images, bool) else None,
        "supports_video": ("video" in lowered) if known else None,
        "supports_audio": ("audio" in lowered) if known else None,
    }


def _record_of(item: Any) -> dict[str, Any]:
    dump = getattr(item, "model_dump", None)
    try:
        rec = dump() if callable(dump) else item
    except Exception:
        return {}
    return rec if isinstance(rec, dict) else {}


def _info_from_id(model_id: str, record: dict[str, Any] | None = None) -> ModelInfo:
    mid = model_id.strip()
    lower = mid.lower()
    vision = "vision" in lower or "image" in lower or "imagine" in lower
    from .spacexai_provider import grok_supports_reasoning_effort, thinking_menu

    return _model_info(
        id=mid,
        display_name=mid,
        supports_vision=vision,
        **_media_fields(record),
        supports_tools=True,
        context_limit=None,
        supports_thinking_effort=grok_supports_reasoning_effort(mid),
        thinking_menu=thinking_menu(mid),
    )


def _fetch_spacexai(api_key: str) -> list[ModelInfo]:
    cache_key = _key_hash(api_key or "")
    hit = _SPACEXAI_MODELS_CACHE.get(cache_key)
    if hit is not None and (time.time() - hit[0]) < _CACHE_TTL_S:
        return list(hit[1])

    models: list[ModelInfo] = []
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=SPACEXAI_BASE_URL)
        listed = client.models.list()
        for item in listed.data or []:
            mid = str(getattr(item, "id", "") or "").strip()
            if mid:
                models.append(_info_from_id(mid, _record_of(item)))
        models.sort(key=lambda m: m.id)
    except Exception as exc:
        _log.warning("SpaceXAI /v1/models unavailable: %s", exc)

    _cache_put(_SPACEXAI_MODELS_CACHE, cache_key, (time.time(), models))
    return list(models)
