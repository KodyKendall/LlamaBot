"""
Tests for per-model multimodal capabilities and their exposure via the
/api/available-models endpoint.

The frontend image auto-switch (move an image-bearing message off a text-only
model onto a vision model) relies on the backend telling it which models can
see images. These tests lock two things:

1. The capability table is correct — notably that BOTH DeepSeek models report
   images: False (deepseek-v4-pro was previously missing from the table and so
   fell through to the permissive default, wrongly reporting images: True).
2. /api/available-models includes a `capabilities` object per model.
"""
import pytest

from app.agents.leonardo.model_capabilities import get_model_capabilities


def test_deepseek_models_report_no_images():
    """Both DeepSeek models are text-only — the auto-switch trigger depends on this."""
    assert get_model_capabilities("deepseek-v4-flash")["images"] is False
    assert get_model_capabilities("deepseek-v4-pro")["images"] is False


def test_gemini_flash_lite_supports_images():
    """gemini-3.1-flash-lite is the image-capable target of the auto-switch."""
    assert get_model_capabilities("gemini-3.1-flash-lite")["images"] is True


def test_qwen_vl_plus_supports_images_and_video():
    """Qwen3-VL Plus is a vision model — images and video, but not native PDF."""
    cap = get_model_capabilities("qwen3-vl-plus")
    assert cap["images"] is True
    assert cap["video"] is True
    assert cap["pdf"] is False


def test_unknown_model_defaults_permissive():
    """Unknown models default to vision-capable so we never spuriously switch."""
    cap = get_model_capabilities("some-future-model")
    assert cap["images"] is True


@pytest.mark.asyncio
async def test_available_models_includes_capabilities(async_client):
    """Every model in the API response carries a capabilities object."""
    response = await async_client.get("/api/available-models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert models, "expected at least one model"

    by_value = {}
    for m in models:
        assert "capabilities" in m, f"{m['value']} missing capabilities"
        caps = m["capabilities"]
        assert set(caps) >= {"images", "video", "pdf"}
        by_value[m["value"]] = caps

    # The two models the auto-switch logic hinges on.
    assert by_value["deepseek-v4-flash"]["images"] is False
    assert by_value["deepseek-v4-pro"]["images"] is False
    assert by_value["gemini-3.1-flash-lite"]["images"] is True
