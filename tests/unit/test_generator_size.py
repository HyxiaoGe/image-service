from __future__ import annotations

import pytest

from src.config import SUPPORTED_MODELS
from src.models.errors import ErrorCode
from src.services.generator import ARK_PRO_LOW_PRICE_PIXELS, GenerationError, _resolve_ark_size


def _dims(size: str) -> tuple[int, int]:
    w, h = size.split("x")
    return int(w), int(h)


@pytest.mark.parametrize(
    ("aspect_ratio", "expected"),
    [
        ("16:9", "2560x1440"),
        ("9:16", "1440x2560"),
        ("1:1", "1920x1920"),
        ("4:3", "2240x1664"),
        ("3:4", "1664x2240"),
    ],
)
def test_resolve_ark_size_standard_ratios(aspect_ratio: str, expected: str):
    # These exact dimensions were verified against the live Ark API for both
    # Seedream 4.0 and 4.5 (4.5 rejects anything under 3,686,400 px).
    assert _resolve_ark_size(aspect_ratio, None) == expected


def test_resolve_ark_size_prefers_aspect_ratio_over_size():
    # The caller's size may be capped (GenerateRequest caps at 2048/side), so the
    # aspect ratio is the reliable orientation signal.
    assert _resolve_ark_size("16:9", "2048x1152") == "2560x1440"


def test_resolve_ark_size_falls_back_to_size_ratio():
    # No aspect ratio → derive orientation from the WxH the caller sent.
    assert _resolve_ark_size(None, "2048x1152") == "2560x1440"


def test_resolve_ark_size_defaults_to_square():
    assert _resolve_ark_size(None, None) == "1920x1920"


def test_resolve_ark_size_ignores_garbage():
    assert _resolve_ark_size("not-a-ratio", "garbage") == "1920x1920"


@pytest.mark.parametrize(
    "aspect_ratio",
    ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "2:3", "3:2", "7:5", "1:4", "4:1", "1:8", "8:1", "1:16", "16:1"],
)
@pytest.mark.parametrize(
    "model", ["doubao-seedream-4-5", "doubao-seedream-5-0-lite", "doubao-seedream-5-0-pro", "doubao-seedream-5-0-flash"]
)
@pytest.mark.parametrize("size", ["1024x1024", "1536x1536", "2048x2048"])
def test_resolve_ark_size_respects_model_limits_and_ratio(aspect_ratio: str, model: str, size: str):
    w, h = _dims(_resolve_ark_size(aspect_ratio, size, model))
    info = SUPPORTED_MODELS[model]
    assert info.ark_min_pixels <= w * h <= info.ark_max_pixels
    assert w % 32 == 0 and h % 32 == 0
    aw, ah = map(int, aspect_ratio.split(":"))
    assert w / h == pytest.approx(aw / ah, rel=0.035)
    assert 1 / 16 <= w / h <= 16


def test_resolve_ark_size_preserves_orientation():
    # Landscape stays landscape, portrait stays portrait.
    w, h = _dims(_resolve_ark_size("16:9", None))
    assert w > h
    w, h = _dims(_resolve_ark_size("9:16", None))
    assert h > w


def test_resolve_ark_size_ignores_oversized_digits():
    # Digit runs past Python's 4300-digit int() limit must fall back, not raise.
    assert _resolve_ark_size("1" * 5000 + ":1", "1" * 5000 + "x1") == "1920x1920"


@pytest.mark.parametrize("model", ["doubao-seedream-5-0-pro", "doubao-seedream-5-0-flash"])
@pytest.mark.parametrize("side", [1024, 1536, 2048])
def test_modern_seedream_honors_square_pixel_budget(model, side):
    assert _resolve_ark_size("1:1", f"{side}x{side}", model) == f"{side}x{side}"


@pytest.mark.parametrize("ratio", ["4:1", "8:1", "16:1", "1:4", "1:8", "1:16"])
def test_extreme_seedream_ratios_are_not_clamped_to_four(ratio):
    w, h = _dims(_resolve_ark_size(ratio, "1536x1536", "doubao-seedream-5-0-pro"))
    aw, ah = map(int, ratio.split(":"))
    assert w / h == aw / ah


@pytest.mark.parametrize("ratio", ["1:1", "16:9", "7:5", "11:10", "8:1", "16:1"])
def test_pro_rounding_does_not_cross_low_price_boundary(ratio):
    w, h = _dims(_resolve_ark_size(ratio, "1615x1615", "doubao-seedream-5-0-pro"))
    assert w * h <= ARK_PRO_LOW_PRICE_PIXELS


@pytest.mark.parametrize("ratio", ["1:17", "17:1"])
def test_seedream_out_of_range_ratio_is_rejected(ratio):
    with pytest.raises(GenerationError) as exc:
        _resolve_ark_size(ratio, None, "doubao-seedream-5-0-flash")
    assert exc.value.code == ErrorCode.INVALID_PARAMS
    assert exc.value.fields == ["aspect_ratio"]
