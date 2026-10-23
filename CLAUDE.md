# image-service

AI image generation middleware. Wraps Google Gemini, Volcengine Seedream and Alibaba Qwen-Image behind a FastAPI REST + MCP interface with Redis caching, rate limiting, and Prometheus metrics.

## Architecture

- `src/api/` -- REST endpoints (generate, models, health)
- `src/services/` -- business logic (generator, cache, metrics, references, reference_search)
- `src/middleware/` -- auth (product service key)
- `src/models/` -- Pydantic request/response schemas
- `src/mcp/` -- FastMCP tool server (generate_image, list_models)
- `src/config.py` -- Settings via pydantic-settings, supported model registry
- `src/main.py` -- App factory, lifespan, CORS, static files, metrics endpoint

## Key constraints

- Python 3.11+ required
- Redis required at runtime (caching + rate limiting + lock)
- LiteLLM Proxy required (`LITELLM_BASE_URL`, `LITELLM_API_KEY` env vars)
- Time budget: 135s per request across all models (see README "总等待预算与取消"); a timed-out model is never resent, connect errors retry within the budget
- Cache TTL: 7 days; thundering-herd protection via Redis locks
- Shared REST/MCP admission: 20 global / 20 per-product / 5 per-subject per 60s; concurrency 6 / 4 / 2, Redis atomic admission, bounded leases

## Dev commands

```bash
uv run uvicorn src.main:create_app --factory --reload --port 8090   # run dev server
uv run pytest                                                        # run tests
/opt/homebrew/bin/ruff check . && /opt/homebrew/bin/ruff format .     # lint + format
python scripts/check_architecture.py                                 # layer dependency check
```

## Config

Env vars loaded from `.env` (see `.env.example`). Key settings: `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `REDIS_URL`, `DEFAULT_MODEL`, `DEFAULT_FALLBACK_MODEL` (switching: see README), `PRODUCTS` (required product-key mapping), `SERVICE_BASE_URL`, `TAVILY_API_KEYS` (reference_subject search; see README).

## Docs index

- `docs/ARCHITECTURE.md` -- layer diagram and data flow
- `docs/CODING_CONVENTIONS.md` -- code style and project rules

## Deployment

Docker Compose on the dev server (self-hosted runner). CI runs ruff lint + architecture check before deploy. Port 8090.
