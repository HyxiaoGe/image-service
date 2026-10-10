#!/usr/bin/env python3
"""Probe how LiteLLM Proxy forwards Gemini image options (aspect ratio / image size).

Sends the same prompt with different payload shapes straight to LiteLLM and prints
the HTTP status, finish_reason and returned image dimensions for each variant.
Used on 2026-10-10 to confirm that a top-level ``imageConfig`` is forwarded (see
fusion-workspace/workspace-docs/image-service-p0-design-2026-10-10.md §1).

Each variant is a real generation and costs money. LiteLLM's response cache does
not key on ``imageConfig``, so every request sends ``cache: {"no-cache": true}``
and a unique prompt suffix; without both, later variants return the first image.

Run inside the dev container to reuse its LITELLM_* env:

    docker cp scripts/probe_gemini_image_config.py image-service:/tmp/probe.py
    docker exec image-service python /tmp/probe.py                 # all variants
    docker exec image-service python /tmp/probe.py camel snake      # selected ones
    docker exec -e PROBE_MODEL=gemini-2.5-flash-image image-service python /tmp/probe.py
"""

from __future__ import annotations

import base64
import os
import struct
import sys
import time

import httpx

PROMPT = "A single red apple on a plain white table, studio photo, no text"

# name -> extra top-level payload fields. Results on 2026-10-10 (LiteLLM v1.93.0,
# gemini-3.1-flash-image-preview): camel / snake / extra_body honoured, the rest ignored.
VARIANTS: dict[str, dict] = {
    "baseline": {},
    "camel": {"imageConfig": {"aspectRatio": "9:16"}},
    "snake": {"image_config": {"aspect_ratio": "9:16"}},
    "extra_body": {"extra_body": {"generationConfig": {"imageConfig": {"aspectRatio": "9:16"}}}},
    "generation_config": {"generationConfig": {"imageConfig": {"aspectRatio": "9:16"}}},
    "openai_size": {"size": "1024x1792"},
    "camel_2x3_2k": {"imageConfig": {"aspectRatio": "2:3", "imageSize": "2K"}},
    "camel_1x1_512": {"imageConfig": {"aspectRatio": "1:1", "imageSize": "512"}},
    "camel_bad_ratio": {"imageConfig": {"aspectRatio": "7:5"}},  # expect 400 listing allowed ratios
}


def image_dims(data: bytes) -> str:
    """Width x height from a PNG or JPEG header, without Pillow."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return f"{w}x{h} png"
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 <= len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return f"{w}x{h} jpeg"
            i += 2 + seg_len
    return f"unknown({data[:4]!r})"


def probe(base: str, key: str, model: str, name: str, extra: dict) -> str:
    payload = {
        "model": f"gemini/{model}",
        "messages": [{"role": "user", "content": f"{PROMPT} (probe {name} {time.time():.0f})"}],
        "modalities": ["text", "image"],
        "cache": {"no-cache": True, "no-store": True},
        **extra,
    }
    started = time.time()
    try:
        resp = httpx.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=300,
        )
    except httpx.HTTPError as e:
        return f"EXC {type(e).__name__}: {e}"
    elapsed = time.time() - started
    if resp.status_code != 200:
        return f"HTTP {resp.status_code} {elapsed:5.1f}s  {' '.join(resp.text.split())[:400]}"
    choice = (resp.json().get("choices") or [{}])[0]
    images = [
        image_dims(base64.b64decode(url.partition(",")[2]))
        for img in (choice.get("message") or {}).get("images") or []
        if (url := img.get("image_url", {}).get("url", "")).startswith("data:")
    ]
    return f"HTTP 200 {elapsed:5.1f}s finish={choice.get('finish_reason')} images={images}"


def main() -> int:
    base = os.environ["LITELLM_BASE_URL"].rstrip("/")
    key = os.environ["LITELLM_API_KEY"]
    model = os.environ.get("PROBE_MODEL", "gemini-3.1-flash-image-preview")
    selected = sys.argv[1:] or list(VARIANTS)
    unknown = [n for n in selected if n not in VARIANTS]
    if unknown:
        print(f"unknown variants: {unknown}; choose from {list(VARIANTS)}", file=sys.stderr)
        return 2
    print(f"model={model}")
    for name in selected:
        print(f"{name:18s} {probe(base, key, model, name, VARIANTS[name])}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
