"""Milestone 2: provider HTTP handling with mocked transports. No network."""

import json

import httpx
import pytest

from recon.config import load_settings
from recon.providers import make_provider
from recon.providers.base import (
    AuthError,
    BadOutput,
    ModelNotFound,
    QuotaExhausted,
    RateLimited,
    ServerError,
    Timeout,
    call_with_retry,
)

KEY = "SENTINEL-KEY-123"
S = load_settings()


def gemini_ok(text='{"columns": [], "rows": []}'):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def chat_ok(text='{"columns": [], "rows": []}'):
    return {"choices": [{"message": {"content": text}}]}


def transport(status, body, headers=None, seen=None):
    def handler(req: httpx.Request):
        if seen is not None:
            seen.append(req)
        if isinstance(body, Exception):
            raise body
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(status, text=content, headers=headers or {})
    return httpx.MockTransport(handler)


def run(name, t):
    p = make_provider(name, S, transport=t)
    return p.extract(image_jpeg=b"\xff\xd8img", prompt="p", schema=None, api_key=KEY,
                     model="m", timeout=5)


@pytest.mark.parametrize("name,ok", [("gemini", gemini_ok()), ("openrouter", chat_ok()),
                                     ("groq", chat_ok())])
def test_success_and_key_placement(name, ok):
    seen = []
    assert "columns" in run(name, transport(200, ok, seen=seen))
    req = seen[0]
    assert KEY not in str(req.url)
    if name == "gemini":
        assert req.headers["x-goog-api-key"] == KEY
        assert "key=" not in str(req.url)
    else:
        assert req.headers["authorization"] == f"Bearer {KEY}"


CASES = [
    (429, {"error": {"message": "Quota exceeded for metric generate_requests_per_day"}},
     QuotaExhausted),
    (429, {"error": {"message": "Rate limit reached, try again in 2s"}}, RateLimited),
    (400, {"error": {"message": "API key not valid. Please pass a valid API key."}}, AuthError),
    (401, {"error": {"message": "Invalid API Key"}}, AuthError),
    (404, {"error": {"message": "models/x is not found for API version v1beta"}}, ModelNotFound),
    (503, {"error": {"message": "overloaded"}}, ServerError),
]


@pytest.mark.parametrize("name", ["gemini", "openrouter", "groq"])
@pytest.mark.parametrize("status,body,exc", CASES)
def test_error_classes(name, status, body, exc):
    with pytest.raises(exc) as ei:
        run(name, transport(status, body))
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)


@pytest.mark.parametrize("name", ["gemini", "groq"])
def test_timeout_and_bad_output(name):
    with pytest.raises(Timeout):
        run(name, transport(200, httpx.ReadTimeout("slow")))
    with pytest.raises(BadOutput):
        run(name, transport(200, "<html>not json</html>"))


def test_retry_honours_retry_after_then_succeeds():
    calls, sleeps, notes = [], [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}},
                                  headers={"retry-after": "1"})
        return httpx.Response(200, json=gemini_ok("{}"))

    p = make_provider("gemini", S, transport=httpx.MockTransport(handler))
    out = call_with_retry(
        lambda: p.extract(image_jpeg=b"x", prompt="p", schema=None, api_key=KEY, model="m",
                          timeout=5),
        sleep=sleeps.append, on_retry=lambda a, m, e: notes.append(f"retrying ({a}/{m})"))
    assert out == "{}" and len(calls) == 3
    assert all(s >= 1 for s in sleeps) and notes == ["retrying (2/4)", "retrying (3/4)"]


def test_retry_gives_up_after_max_and_never_retries_auth():
    calls = []

    def flaky():
        calls.append(1)
        raise ServerError("gemini", "boom", 500)

    with pytest.raises(ServerError):
        call_with_retry(flaky, max_attempts=4, sleep=lambda s: None)
    assert len(calls) == 4

    calls.clear()

    def auth():
        calls.append(1)
        raise AuthError("gemini", "bad", 401)

    with pytest.raises(AuthError):
        call_with_retry(auth, sleep=lambda s: None)
    assert len(calls) == 1


def test_list_models_openrouter_filters_free_vision():
    body = {"data": [
        {"id": "a:free", "architecture": {"input_modalities": ["text", "image"]},
         "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "b", "architecture": {"input_modalities": ["text", "image"]},
         "pricing": {"prompt": "0.001", "completion": "0.002"}},
        {"id": "c:free", "architecture": {"input_modalities": ["text"]},
         "pricing": {"prompt": "0", "completion": "0"}},
    ]}
    p = make_provider("openrouter", S, transport=transport(200, body))
    assert p.list_models(api_key=KEY, timeout=5) == ["a:free"]


def test_test_connection_gemini():
    p = make_provider("gemini", S, transport=transport(200, gemini_ok("OK")))
    assert p.test_connection(api_key=KEY, model="m", timeout=5) == "ok"
