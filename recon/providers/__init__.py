"""Provider registry."""

from __future__ import annotations

import httpx

from recon.config import Settings
from recon.providers.gemini import GeminiProvider
from recon.providers.groq import GroqProvider
from recon.providers.openrouter import OpenRouterProvider

PROVIDER_LABELS = {"gemini": "Gemini", "openrouter": "OpenRouter", "groq": "Groq",
                   "offline": "Offline (cached extraction)"}


def make_provider(name: str, settings: Settings, transport: httpx.BaseTransport | None = None):
    """Build a provider instance by name. Instances never hold keys."""
    cfg = settings.providers.get(name)
    cls = {"gemini": GeminiProvider, "openrouter": OpenRouterProvider, "groq": GroqProvider}[name]
    return cls(cfg.base_url, transport=transport)
