#!/usr/bin/env python3
"""Probe whether LiteLLM Proxy forwards Google Search grounding to Gemini image models.

Sends the same prompt with different tool payload shapes straight to LiteLLM and
prints, per variant: HTTP status, finish_reason, image dimensions and any grounding
metadata found in the response (search queries, source count). Images are written
to OUT_DIR so variants can be compared by eye.

Each variant is a real generation and costs money. Every request disables
LiteLLM's response cache and carries a unique suffix so variants don't collide.

Run inside the dev container to reuse its LITELLM_* env:

    docker cp scripts/probe_gemini_grounding.py image-service:/tmp/probe.py
    docker exec image-service python /tmp/probe.py                    # all variants
    docker exec image-service python /tmp/probe.py off google_search  # selected ones
    docker exec -e PROBE_MODEL=gemini-3-pro-image-preview image-service python /tmp/probe.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import httpx

PROMPT = os.environ.get("PROBE_PROMPT", "Official product marketing photo of the iPhone 18 Pro, back view")

# name -> extra top-level payload fields
VARIANTS: dict[str, dict] = {
    "off": {},
    "googleSearch": {"tools": [{"googleSearch": {}}]},
    "google_search": {"tools": [{"google_search": {}}]},
    "web_search_options": {"web_search_options": {}},
    "image_search": {"tools": [{"googleSearch": {"searchTypes": {"webSearch": {}, "imageSearch": {}}}}]},
    "image_search_snake": {"tools": [{"google_search": {"search_types": {"web_search": {}, "image_search": {}}}}]},
}


def find_grounding(obj, path=""):
    """Yield (path, value) for any key mentioning grounding/search anywhere in the response."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if any(s in k.lower() for s in ("grounding", "search", "citation", "annotation")):
                yield p, v
            else:
                yield from find_grounding(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_grounding(v, f"{path}[{i}]")


def summarize(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    queries = []
    for key in ("webSearchQueries", "web_search_queries", "imageSearchQueries", "image_search_queries"):
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and item.get(key):
                queries += item[key]
    chunks = text.count('"uri"')
    return f"queries={queries} sources~{chunks} raw={text[:300]}"


def probe(base: str, key: str, model: str, name: str, extra: dict, out_dir: str) -> str:
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
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    saved = []
    for i, img in enumerate(message.get("images") or []):
        url = img.get("image_url", {}).get("url", "")
        if url.startswith("data:"):
            ext = "png" if "png" in url[:30] else "jpg"
            path = os.path.join(out_dir, f"{model}-{name}-{i}.{ext}")
            with open(path, "wb") as f:
                f.write(base64.b64decode(url.partition(",")[2]))
            saved.append(path)
    lines = [f"HTTP 200 {elapsed:5.1f}s finish={choice.get('finish_reason')} images={saved}"]
    lines += [f"    {p}: {summarize(v)}" for p, v in find_grounding(data)]
    if message.get("content"):
        lines.append(f"    text: {' '.join(str(message['content']).split())[:200]}")
    return "\n".join(lines)


def main() -> int:
    base = os.environ["LITELLM_BASE_URL"].rstrip("/")
    key = os.environ["LITELLM_API_KEY"]
    model = os.environ.get("PROBE_MODEL", "gemini-3.1-flash-image-preview")
    out_dir = os.environ.get("OUT_DIR", "/tmp/probe-grounding")
    os.makedirs(out_dir, exist_ok=True)
    selected = sys.argv[1:] or list(VARIANTS)
    unknown = [n for n in selected if n not in VARIANTS]
    if unknown:
        print(f"unknown variants: {unknown}; choose from {list(VARIANTS)}", file=sys.stderr)
        return 2
    print(f"model={model}")
    for name in selected:
        print(f"{name:20s} {probe(base, key, model, name, VARIANTS[name], out_dir)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
