"""Shared OpenAI-compatible chat/completions client (OpenRouter, Groq)."""

from __future__ import annotations

import httpx

from recon.providers.base import BadOutput, BadRequest, b64, get_json, guard_key, post_json


class OpenAICompatProvider:
    """Vision provider speaking the OpenAI chat/completions format."""

    name = "openai_compat"
    extra_headers: dict[str, str] = {}

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {guard_key(api_key)}",
                "Content-Type": "application/json", **self.extra_headers}

    def _chat(self, api_key: str, payload: dict, timeout: float) -> str:
        url = f"{self.base_url}/chat/completions"
        data = post_json(self.name, url, self._headers(api_key), payload, timeout, self.transport)
        if data.get("error"):
            raise BadOutput(self.name, str(data["error"].get("message", "error in body")))
        choices = data.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") if choices else None) or ""
        if not text.strip():
            raise BadOutput(self.name, "empty response")
        return text

    def extract(self, *, image_jpeg: bytes, prompt: str, schema: dict | None,
                api_key: str, model: str, timeout: float) -> str:
        """Send image as a data URL; ask for JSON output, falling back if unsupported."""
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64(image_jpeg)}"}},
            ]}],
            "response_format": {"type": "json_object"},
        }
        try:
            return self._chat(api_key, payload, timeout)
        except BadRequest as e:
            if "response_format" not in e.safe_message and "json" not in e.safe_message.lower():
                raise
            payload.pop("response_format")
            return self._chat(api_key, payload, timeout)

    def test_connection(self, *, api_key: str, model: str, timeout: float) -> str:
        """Tiny text call to check key, quota and model."""
        payload = {"model": model, "max_tokens": 5,
                   "messages": [{"role": "user", "content": "Reply with the word OK."}]}
        self._chat(api_key, payload, timeout)
        return "ok"

    def list_models(self, *, api_key: str, timeout: float) -> list[str]:
        """All model ids (subclasses filter)."""
        data = get_json(self.name, f"{self.base_url}/models", self._headers(api_key), timeout,
                        self.transport)
        return sorted(m.get("id", "") for m in data.get("data", []))
