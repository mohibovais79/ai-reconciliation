"""Groq (Llama vision models), OpenAI-compatible."""

from __future__ import annotations

from recon.providers.openai_compat import OpenAICompatProvider


class GroqProvider(OpenAICompatProvider):
    """Groq chat/completions."""

    name = "groq"

    def list_models(self, *, api_key: str, timeout: float) -> list[str]:
        """Model ids that look vision-capable (Groq's list has no modality field)."""
        ids = super().list_models(api_key=api_key, timeout=timeout)
        vision = [i for i in ids if any(t in i.lower() for t in ("vision", "llama-4", "scout",
                                                                  "maverick"))]
        return vision or ids
