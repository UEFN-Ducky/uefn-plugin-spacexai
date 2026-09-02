"""SpaceXAI / xAI model list fetch for this gateway plugin."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from backend.agent.model_fetch import ModelInfo, _cache_put

from .spacexai_provider import SPACEXAI_BASE_URL

_log = logging.getLogger(__name__)
_CACHE_MAX = 512
_CACHE_TTL_S = 6 * 3600.0

_SPACEXAI_MODELS_CACHE: dict[str, tuple[float, list[ModelInfo]]] = {}

# Fallback when /v1/models is unavailable (console.x.ai).
_FALLBACK_MODELS: tuple[tuple[str, bool, int | None], ...] = (
    ("grok-4.5", False, None),
    ("grok-4", False, None),
    ("grok-3", False, None),
    ("grok-3-mini", False, None),
    ("grok-2-vision-1212", True, None),
    ("grok-2-image-1212", True, None),
)


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()


def clear_model_cache() -> None:
    _SPACEXAI_MODELS_CACHE.clear()


def fetch_models(api_key: str, **_kw: Any) -> list[ModelInfo]:
    return _fetch_spacexai(api_key)


def _fallback_models() -> list[ModelInfo]:
    return [
        ModelInfo(
            id=mid,
            display_name=mid,
            supports_vision=vision,
            supports_tools=True,
            context_limit=ctx,
        )
        for mid, vision, ctx in _FALLBACK_MODELS
    ]


def _info_from_id(model_id: str) -> ModelInfo:
    mid = model_id.strip()
    lower = mid.lower()
    vision = "vision" in lower or "image" in lower or "imagine" in lower
    return ModelInfo(
        id=mid,
        display_name=mid,
        supports_vision=vision,
        supports_tools=True,
        context_limit=None,
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
                models.append(_info_from_id(mid))
        models.sort(key=lambda m: m.id)
    except Exception as exc:
        _log.warning("SpaceXAI /v1/models unavailable: %s", exc)
        models = _fallback_models()

    if not models:
        models = _fallback_models()

    _cache_put(_SPACEXAI_MODELS_CACHE, cache_key, (time.time(), models))
    return list(models)
