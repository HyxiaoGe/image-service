# image-service

AI image generation middleware. Wraps Google Gemini API behind a FastAPI REST + MCP interface with Redis caching, rate limiting, and Prometheus metrics.

## Architecture

- `src/api/` -- REST endpoints (generate, models, health)
- `src/services/` -- business logic (generator, cache, metrics)
- `src/middleware/` -- auth (Bearer token) and rate limiting
- `src/models/` -- Pydantic request/response schemas
- `src/mcp/` -- FastMCP tool server (generate_image, list_models)
- `src/config.py` -- Settings via pydantic-settings, supported model registry
- `src/main.py` -- App factory, lifespan, CORS, static files, metrics endpoint

## Key constraints

- Python 3.11+ required
- Redis required at runtime (caching + rate limiting + lock)
- LiteLLM Proxy required (`LITELLM_BASE_URL`, `LITELLM_API_KEY` env vars)
- Upstream timeout: 300s per generation request, 2 retries on timeout/connect errors
- Cache TTL: 7 days; thundering-herd protection via Redis locks
- Rate limits: 20 global / 5 per-IP per minute window

## Dev commands

```bash
uv run uvicorn src.main:create_app --factory --reload --port 8090   # run dev server
uv run pytest                                                        # run tests
/opt/homebrew/bin/ruff check . && /opt/homebrew/bin/ruff format .     # lint + format
python scripts/check_architecture.py                                 # layer dependency check
```

## Config

Env vars loaded from `.env` (see `.env.example`). Key settings: `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `REDIS_URL`, `DEFAULT_MODEL`, `DEFAULT_FALLBACK_MODEL` (switching: see README), `API_KEY` (optional auth), `SERVICE_BASE_URL`.

## Docs index

- `docs/ARCHITECTURE.md` -- layer diagram and data flow
- `docs/CODING_CONVENTIONS.md` -- code style and project rules

## Deployment

Docker Compose on the dev server (self-hosted runner). CI runs ruff lint + architecture check before deploy. Port 8090.
