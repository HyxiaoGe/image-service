from __future__ import annotations

import json

import pytest

from src.config import SUPPORTED_MODELS, Settings


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_platform_prefixed_billing_and_product_key_configuration(tmp_path, monkeypatch, source):
    values = {
        "ARK_BILLING_ENABLED": "true",
        "ARK_BILLING_ACCESS_KEY": "ark-billing-test-ak",
        "ARK_BILLING_SECRET_KEY": "ark-billing-test-sk",
        "ARK_BILLING_REGION": "cn-beijing",
        "ARK_BILLING_PRODUCT_CODE": "Doubao-Seedream",
        "DASHSCOPE_API_KEY": "separate-dashscope-test-key",
        "PRODUCTS": json.dumps(
            {
                "fusion": {
                    "service_key_env": "FUSION_SERVICE_KEY",
                    "ark_key_env": "ARK_FUSION_API_KEY",
                    "ark_key_alias": "fusion",
                    "ark_resource_id": "apikey-test-fusion",
                }
            }
        ),
    }
    for key in values:
        monkeypatch.delenv(key, raising=False)
    if source == "environment":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        settings = Settings(_env_file=None)
    else:
        path = tmp_path / ".env"
        path.write_text("# 平台配置\n" + "\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
        settings = Settings(_env_file=path)
    assert settings.ark_billing_enabled is True
    assert settings.ark_billing_access_key.get_secret_value() == values["ARK_BILLING_ACCESS_KEY"]
    assert settings.ark_billing_secret_key.get_secret_value() == values["ARK_BILLING_SECRET_KEY"]
    assert settings.ark_billing_region == values["ARK_BILLING_REGION"]
    assert settings.ark_billing_product_code == values["ARK_BILLING_PRODUCT_CODE"]
    assert settings.dashscope_api_key == values["DASHSCOPE_API_KEY"]
    assert settings.products["fusion"].ark_key_env == "ARK_FUSION_API_KEY"
    assert values["ARK_BILLING_SECRET_KEY"] not in repr(settings)


def test_seedream_model_registered_as_images_endpoint():
    model = SUPPORTED_MODELS["doubao-seedream-4-0"]
    assert model.endpoint_type == "images"
    assert model.provider == "Volcano"
    assert model.name == "Doubao Seedream 4.0"


def test_seedream_45_model_registered_as_images_endpoint():
    model = SUPPORTED_MODELS["doubao-seedream-4-5"]
    assert model.endpoint_type == "images"
    assert model.provider == "Volcano"
    assert model.name == "Doubao Seedream 4.5"


def test_gemini_models_default_to_chat_endpoint():
    assert SUPPORTED_MODELS["gemini-3-pro-image-preview"].endpoint_type == "chat"


def test_ark_settings_defaults():
    s = Settings(_env_file=None)
    assert s.auth_mode == "required"
    assert s.ark_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    # Default Seedream version is pinned to 4.5 (better Chinese text rendering).
    assert s.ark_model_id == "doubao-seedream-4-5-251128"


def test_seedream_50_models_pin_wire_ids_and_skip_sequential_param():
    assert SUPPORTED_MODELS["doubao-seedream-5-0-pro"].ark_model_id == "doubao-seedream-5-0-pro-260628"
    assert SUPPORTED_MODELS["doubao-seedream-5-0-flash"].ark_model_id == "doubao-seedream-5-0-flash-260915"
    assert SUPPORTED_MODELS["doubao-seedream-5-0-lite"].ark_model_id == "doubao-seedream-5-0-260128"
    for model_id, info in SUPPORTED_MODELS.items():
        if info.endpoint_type == "images":
            assert info.sequential_param == model_id.startswith("doubao-seedream-4-")
