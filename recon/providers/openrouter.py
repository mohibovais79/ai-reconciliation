"""OpenRouter (free vision models carry a `:free` suffix)."""

from __future__ import annotations

from recon.providers.base import get_json
from recon.providers.openai_compat import OpenAICompatProvider


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter chat/completions."""

    name = "openrouter"
    extra_headers = {"X-Title": "Register Reconciliation"}

    def list_models(self, *, api_key: str, timeout: float) -> list[str]:
        """Free models that accept image input (best effort)."""
        data = get_json(self.name, f"{self.base_url}/models", self._headers(api_key), timeout,
                        self.transport)
        out = []
        for m in data.get("data", []):
            mods = (m.get("architecture") or {}).get("input_modalities") or []
            price = m.get("pricing") or {}
            free = all(str(price.get(k, "0")) in ("0", "0.0") for k in ("prompt", "completion"))
            if "image" in mods and free:
                out.append(m["id"])
        return sorted(out)
