from __future__ import annotations

import pytest

from src.services.generator import ARK_MAX_PIXELS, ARK_MIN_PIXELS, _resolve_ark_size


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
    ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "2:3", "3:2", "7:5", "1:4", "4:1", "1:8", "8:1", "1:30"],
)
def test_resolve_ark_size_always_meets_pixel_floor(aspect_ratio: str):
    w, h = _dims(_resolve_ark_size(aspect_ratio, None))
    assert w * h >= ARK_MIN_PIXELS
    # Seedream 5.0 Pro / Flash reject areas above 4,624,220 px.
    assert w * h <= ARK_MAX_PIXELS
    # Every side stays on the 32px alignment grid and within Ark's bounds.
    assert w % 32 == 0 and h % 32 == 0
    assert 1024 <= w <= 4096 and 1024 <= h <= 4096


def test_resolve_ark_size_preserves_orientation():
    # Landscape stays landscape, portrait stays portrait.
    w, h = _dims(_resolve_ark_size("16:9", None))
    assert w > h
    w, h = _dims(_resolve_ark_size("9:16", None))
    assert h > w


def test_resolve_ark_size_ignores_oversized_digits():
    # Digit runs past Python's 4300-digit int() limit must fall back, not raise.
    assert _resolve_ark_size("1" * 5000 + ":1", "1" * 5000 + "x1") == "1920x1920"
