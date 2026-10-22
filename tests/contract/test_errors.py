"""Contract tests for structured error codes on POST /v1/generate."""

from __future__ import annotations

import httpx
import pytest

from src.services import generator
from tests.conftest import MockTransport


class RaisingTransport(httpx.AsyncBaseTransport):
    """Transport that raises the given httpx exception on every request."""

    def __init__(self, exc: type[httpx.TransportError]):
        self._exc = exc
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        raise self._exc("boom", request=request)


def gemini_no_image(finish_reason: str = "stop", content: str | None = None) -> dict:
    return {
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}]
    }


def use_transport(app, transport: httpx.AsyncBaseTransport) -> None:
    app.state.http_client = httpx.AsyncClient(transport=transport)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(generator.asyncio, "sleep", _no_sleep)


async def post(client: httpx.AsyncClient, **body) -> httpx.Response:
    return await client.post("/v1/generate", json={"prompt": "a sunset", **body})


def assert_error(resp: httpx.Response, status: int, code: str, retryable: bool) -> dict:
    assert resp.status_code == status, resp.text
    data = resp.json()
    assert data["error"] == code
    assert data["retryable"] is retryable
    assert data["message"]
    return data


# --- Gemini (LiteLLM) -------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_content_filter(client, app):
    use_transport(app, MockTransport(response_data=gemini_no_image("content_filter", "I can't make that.")))
    data = assert_error(await post(client), 422, "content_filtered", False)
    assert data["model"] == "gemini-3-pro-image-preview"
    # Upstream wording stays in logs, never in the caller-facing message.
    assert "can't make that" not in data["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Request blocked due to SAFETY", "PROHIBITED_CONTENT detected"])
async def test_gemini_safety_400(client, app, message):
    use_transport(app, MockTransport(response_data={"error": {"message": message}}, status_code=400))
    assert_error(await post(client), 422, "content_filtered", False)


@pytest.mark.asyncio
async def test_gemini_rejects_aspect_ratio(client, app):
    msg = "aspect_ratio must be one of '1:1', '16:9'"
    use_transport(app, MockTransport(response_data={"error": {"message": msg}}, status_code=400))
    assert_error(await post(client), 422, "invalid_params", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_data",
    [gemini_no_image("stop"), {"choices": []}],
    ids=["stop-without-image", "no-choices"],
)
async def test_gemini_no_image(client, app, response_data):
    use_transport(app, MockTransport(response_data=response_data))
    assert_error(await post(client), 502, "no_image_returned", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 429, 500, 503])
async def test_gemini_unavailable(client, app, status):
    use_transport(app, MockTransport(response_data={"error": {"message": "quota exceeded"}}, status_code=status))
    data = assert_error(await post(client), 502, "upstream_unavailable", True)
    assert "quota exceeded" not in data["message"]


@pytest.mark.asyncio
async def test_gemini_other_4xx(client, app):
    use_transport(app, MockTransport(response_data={"error": {"message": "bad key"}}, status_code=401))
    assert_error(await post(client), 502, "upstream_error", False)


@pytest.mark.asyncio
async def test_upstream_read_timeout_is_not_resent(client, app):
    transport = RaisingTransport(httpx.ReadTimeout)
    use_transport(app, transport)
    assert_error(await post(client), 504, "upstream_timeout", True)
    assert transport.call_count == 1


@pytest.mark.asyncio
async def test_upstream_connect_error_after_retries(client, app):
    use_transport(app, RaisingTransport(httpx.ConnectError))
    assert_error(await post(client), 502, "upstream_unavailable", True)


# --- Seedream (Ark) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_seedream_missing_key(client, app):
    app.state.settings.ark_api_key = ""
    data = assert_error(await post(client, model="doubao-seedream-4-5"), 503, "config_missing", False)
    assert data["model"] == "doubao-seedream-4-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("ark_code", ["InputTextSensitiveContentDetected", "OutputImageSensitiveContentDetected"])
async def test_seedream_sensitive_content(client, app, ark_code):
    app.state.settings.ark_api_key = "k"
    body = {"error": {"code": ark_code, "message": "sensitive"}}
    use_transport(app, MockTransport(response_data=body, status_code=400))
    assert_error(await post(client, model="doubao-seedream-4-5"), 422, "content_filtered", False)


@pytest.mark.asyncio
async def test_seedream_no_image(client, app):
    app.state.settings.ark_api_key = "k"
    use_transport(app, MockTransport(response_data={"data": []}))
    assert_error(await post(client, model="doubao-seedream-4-5"), 502, "no_image_returned", True)


@pytest.mark.asyncio
async def test_seedream_other_4xx(client, app):
    app.state.settings.ark_api_key = "k"
    body = {"error": {"code": "InvalidParameter", "message": "bad size"}}
    use_transport(app, MockTransport(response_data=body, status_code=400))
    assert_error(await post(client, model="doubao-seedream-4-5"), 502, "upstream_error", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("ark_code", ["AccountOverdueError", "OperationDenied.ServiceOverdue"])
async def test_seedream_403_overdue_is_unavailable(client, app, ark_code):
    app.state.settings.ark_api_key = "k"
    body = {"error": {"code": ark_code, "message": "overdue"}}
    use_transport(app, MockTransport(response_data=body, status_code=403))
    assert_error(await post(client, model="doubao-seedream-5-0-flash"), 502, "upstream_unavailable", True)


@pytest.mark.asyncio
async def test_seedream_403_permission_error_is_not_unavailable(client, app):
    app.state.settings.ark_api_key = "k"
    body = {"error": {"code": "AccessDenied", "message": "denied"}}
    use_transport(app, MockTransport(response_data=body, status_code=403))
    assert_error(await post(client, model="doubao-seedream-5-0-flash"), 502, "upstream_error", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.WriteTimeout])
async def test_seedream_submitted_timeout_is_not_retried(client, app, exc_type):
    app.state.settings.ark_api_key = "k"
    transport = RaisingTransport(exc_type)
    use_transport(app, transport)
    assert_error(await post(client, model="doubao-seedream-5-0-flash"), 504, "upstream_timeout", True)
    assert transport.call_count == 1


@pytest.mark.asyncio
async def test_seedream_connection_timeout_still_retries(client, app):
    app.state.settings.ark_api_key = "k"
    transport = RaisingTransport(httpx.ConnectTimeout)
    use_transport(app, transport)
    assert_error(await post(client, model="doubao-seedream-5-0-flash"), 504, "upstream_timeout", True)
    assert transport.call_count == generator.MAX_RETRIES + 1


# --- Request-level ----------------------------------------------------------


@pytest.mark.asyncio
async def test_model_not_found(client):
    data = assert_error(await post(client, model="nope"), 400, "model_not_found", False)
    assert "nope" in data["message"]


@pytest.mark.asyncio
async def test_validation_error_shape(client):
    resp = await post(client, aspect_ratio="wide")
    data = assert_error(resp, 422, "invalid_params", False)
    assert data["fields"] == ["aspect_ratio"]
    assert "detail" not in data


@pytest.mark.asyncio
async def test_unexpected_exception_is_internal_error(client, app, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("secret stack detail")

    monkeypatch.setattr("src.api.generate.handle_generate", boom)
    data = assert_error(await post(client), 500, "internal_error", False)
    assert "secret" not in data["message"]


@pytest.mark.asyncio
async def test_success_reports_requested_model(client):
    resp = await post(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == data["requested_model"] == "gemini-3-pro-image-preview"
    assert data["fallback_used"] is False


@pytest.mark.asyncio
async def test_other_transport_error_is_unavailable(client, app):
    """Errors like a dropped connection mid-response are upstream problems, not internal ones."""
    transport = RaisingTransport(httpx.RemoteProtocolError)
    use_transport(app, transport)
    assert_error(await post(client), 502, "upstream_unavailable", True)
    assert transport.call_count == 1  # not retried: only timeouts/connect errors are


class TextTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway hiccup</html>")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-3-pro-image-preview", "doubao-seedream-4-5"])
async def test_non_json_200_is_no_image(client, app, model):
    app.state.settings.ark_api_key = "k"
    use_transport(app, TextTransport())
    assert_error(await post(client, model=model), 502, "no_image_returned", True)
