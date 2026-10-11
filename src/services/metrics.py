from __future__ import annotations

from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "image_requests_total",
    "Total image generation requests",
    ["model", "status"],
)

GENERATION_DURATION = Histogram(
    "image_generation_duration_seconds",
    "Time spent generating an image (excludes cache hits)",
    ["model"],
    buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300),
)

CACHE_HITS = Counter(
    "image_cache_hits_total",
    "Total cache hits for image generation",
)

FALLBACKS = Counter(
    "image_fallback_total",
    "Fallback attempts after the primary model's content filter blocked a request",
    ["from_model", "to_model", "outcome"],
)

GENERATION_ERRORS = Counter(
    "image_generation_errors_total",
    "Total image generation errors",
    ["model", "error_type"],
)

REFERENCE_SEARCHES = Counter(
    "image_reference_search_total",
    "Automatic reference image searches (reference_subject) by outcome",
    ["status"],
)
