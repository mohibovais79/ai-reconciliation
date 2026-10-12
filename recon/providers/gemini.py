"""Google Gemini via REST generateContent. Key goes in the x-goog-api-key header only."""

from __future__ import annotations

import httpx

from recon.providers.base import BadOutput, b64, get_json, guard_key, post_json


class GeminiProvider:
    """Gemini vision provider (Google AI Studio)."""

    name = "gemini"

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": guard_key(api_key), "Content-Type": "application/json"}

    def _generate(self, api_key: str, model: str, payload: dict, timeout: float) -> str:
        url = f"{self.base_url}/models/{model}:generateContent"
        data = post_json(self.name, url, self._headers(api_key), payload, timeout, self.transport)
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise BadOutput(self.name, f"empty response ({reason})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise BadOutput(self.name, f"no text (finishReason={cands[0].get('finishReason')})")
        return text

    def extract(self, *, image_jpeg: bytes, prompt: str, schema: dict | None,
                api_key: str, model: str, timeout: float) -> str:
        """Send one page image and the prompt; return the model's text."""
        gen_cfg: dict = {"responseMimeType": "application/json", "temperature": 0,
                         "maxOutputTokens": 32768}
        payload = {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64(image_jpeg)}},
                {"text": prompt},
            ]}],
            "generationConfig": gen_cfg,
        }
        return self._generate(api_key, model, payload, timeout)

    def test_connection(self, *, api_key: str, model: str, timeout: float) -> str:
        """Tiny text call to check key, quota and model."""
        payload = {"contents": [{"role": "user", "parts": [{"text": "Reply with the word OK."}]}]}
        self._generate(api_key, model, payload, timeout)
        return "ok"

    def list_models(self, *, api_key: str, timeout: float) -> list[str]:
        """Models on this key that support generateContent (best effort)."""
        data = get_json(self.name, f"{self.base_url}/models?pageSize=200",
                        self._headers(api_key), timeout, self.transport)
        out = []
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                out.append(m.get("name", "").removeprefix("models/"))
        return sorted(out)
