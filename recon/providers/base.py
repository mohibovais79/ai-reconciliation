"""Provider protocol, error classes, HTTP classification and retry with backoff.

API shapes checked against provider docs on 2026-10-12:
- Gemini generateContent: POST {base}/models/{model}:generateContent, key in `x-goog-api-key`.
- OpenRouter / Groq: OpenAI-compatible POST {base}/chat/completions, `Authorization: Bearer`.
"""

from __future__ import annotations

import base64
import email.utils
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from recon.logging_utils import redact, register_secret


class ProviderError(Exception):
    """Base error. Messages are always redacted and never contain headers or URLs."""

    retryable = False
    kind = "ProviderError"

    def __init__(self, provider: str, message: str, status: int | None = None,
                 retry_after: float | None = None):
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.safe_message = redact(message)[:400]
        super().__init__(f"{provider}: {self.kind}: {self.safe_message}")

    def user_message(self) -> str:
        """Plain-language explanation for the UI."""
        return f"{self.provider} error: {self.safe_message}"


class AuthError(ProviderError):
    kind = "AuthError"

    def user_message(self) -> str:
        return f"Key rejected by {self.provider}. Check the key or switch provider."


class QuotaExhausted(ProviderError):
    kind = "QuotaExhausted"

    def user_message(self) -> str:
        return f"The {self.provider} key is out of quota for now ({self.safe_message[:120]})."


class RateLimited(ProviderError):
    kind = "RateLimited"
    retryable = True


class ModelNotFound(ProviderError):
    kind = "ModelNotFound"

    def user_message(self) -> str:
        return f"{self.provider} does not offer that model (or not on this key). Pick another."


class Timeout(ProviderError):
    kind = "Timeout"
    retryable = True


class ServerError(ProviderError):
    kind = "ServerError"
    retryable = True


class BadRequest(ProviderError):
    kind = "BadRequest"


class BadOutput(ProviderError):
    kind = "BadOutput"


QUOTA_HINTS = ("per day", "perday", "per-day", "daily", "free-models-per-day", "(rpd)", "(tpd)",
               "quota exceeded for metric", "billing", "credits")
MODEL_HINTS = ("model not found", "not found for api version", "does not exist",
               "is not supported", "no endpoints found", "model_not_found", "decommissioned",
               "invalid model")
AUTH_HINTS = ("api key not valid", "api_key_invalid", "invalid api key", "unauthorized",
              "permission denied", "invalid_api_key")


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time()) if dt else None


def classify_http(provider: str, status: int, body: str, headers: httpx.Headers) -> ProviderError:
    """Map an HTTP error response to a typed ProviderError."""
    low = body.lower()
    msg = body.strip()[:400] or f"HTTP {status}"
    retry_after = parse_retry_after(headers.get("retry-after"))
    if status == 429:
        if any(h in low for h in QUOTA_HINTS):
            return QuotaExhausted(provider, msg, status)
        return RateLimited(provider, msg, status, retry_after)
    if status in (401, 403) or any(h in low for h in AUTH_HINTS):
        return AuthError(provider, msg, status)
    if status == 404 or any(h in low for h in MODEL_HINTS):
        return ModelNotFound(provider, msg, status)
    if status == 408:
        return Timeout(provider, msg, status)
    if status >= 500:
        return ServerError(provider, msg, status, retry_after)
    return BadRequest(provider, msg, status)


def post_json(provider: str, url: str, headers: dict[str, str], payload: dict[str, Any],
              timeout: float, transport: httpx.BaseTransport | None = None) -> dict:
    """POST JSON with a fresh client (no shared state holding keys); raise typed errors."""
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise Timeout(provider, f"no response within {timeout:.0f}s") from None
    except httpx.HTTPError as e:
        raise ServerError(provider, f"network error ({type(e).__name__})") from None
    if resp.status_code >= 400:
        raise classify_http(provider, resp.status_code, resp.text, resp.headers)
    try:
        return resp.json()
    except ValueError:
        raise BadOutput(provider, "response was not JSON") from None


def get_json(provider: str, url: str, headers: dict[str, str], timeout: float,
             transport: httpx.BaseTransport | None = None) -> dict:
    """GET JSON with typed errors (used by list-models)."""
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.get(url, headers=headers)
    except httpx.TimeoutException:
        raise Timeout(provider, f"no response within {timeout:.0f}s") from None
    except httpx.HTTPError as e:
        raise ServerError(provider, f"network error ({type(e).__name__})") from None
    if resp.status_code >= 400:
        raise classify_http(provider, resp.status_code, resp.text, resp.headers)
    return resp.json()


def b64(data: bytes) -> str:
    """Base64-encode bytes as ASCII."""
    return base64.b64encode(data).decode("ascii")


class VisionProvider(Protocol):
    """A vision model that turns a page image + prompt into raw text (JSON expected)."""

    name: str

    def extract(self, *, image_jpeg: bytes, prompt: str, schema: dict | None,
                api_key: str, model: str, timeout: float) -> str: ...

    def test_connection(self, *, api_key: str, model: str, timeout: float) -> str: ...

    def list_models(self, *, api_key: str, timeout: float) -> list[str]: ...


def call_with_retry(fn: Callable[[], str], *, max_attempts: int = 4, base_s: float = 1.5,
                    max_s: float = 30.0, on_retry: Callable[[int, int, ProviderError], None]
                    | None = None, sleep: Callable[[float], None] = time.sleep) -> str:
    """Run fn, retrying retryable ProviderErrors with exponential backoff + jitter."""
    attempt = 1
    while True:
        try:
            return fn()
        except ProviderError as e:
            if not e.retryable or attempt >= max_attempts:
                raise
            delay = min(max_s, base_s * 2 ** (attempt - 1)) * (0.5 + random.random())
            if e.retry_after is not None:
                delay = min(max_s * 2, max(delay, e.retry_after))
            if on_retry:
                on_retry(attempt + 1, max_attempts, e)
            sleep(delay)
            attempt += 1


def guard_key(api_key: str) -> str:
    """Register the key with the log redactor and return it unchanged."""
    register_secret(api_key)
    return api_key
