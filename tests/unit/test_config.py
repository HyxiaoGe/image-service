from __future__ import annotations

from src.config import SUPPORTED_MODELS, Settings


def test_seedream_model_registered_as_images_endpoint():
    model = SUPPORTED_MODELS["doubao-seedream-4-0"]
    assert model.endpoint_type == "images"
    assert model.provider == "Volcano"
    assert model.name == "Doubao Seedream 4.0"


def test_gemini_models_default_to_chat_endpoint():
    assert SUPPORTED_MODELS["gemini-3-pro-image-preview"].endpoint_type == "chat"


def test_ark_settings_defaults():
    s = Settings(_env_file=None)
    assert s.ark_api_key == ""
    assert s.ark_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert s.ark_model_id == "doubao-seedream-4-0-250828"
