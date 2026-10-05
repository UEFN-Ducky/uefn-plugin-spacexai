"""SpaceXAI model records report media limits only when the API provides them."""

from __future__ import annotations

from backend.model_fetch import _info_from_id


def test_media_fields_from_record():
    info = _info_from_id("grok-x", {"max_images": 10, "input_modalities": ["text", "image", "Video"]})
    assert (info.max_images, info.supports_video, info.supports_audio) == (10, True, False)


def test_audio_from_modalities():
    assert _info_from_id("grok-x", {"input_modalities": ["text", "audio"]}).supports_audio is True


def test_media_fields_unknown_without_record_data():
    for rec in (None, {}, {"id": "grok-x"}):
        info = _info_from_id("grok-x", rec)
        assert (info.max_images, info.supports_video, info.supports_audio) == (None, None, None)
