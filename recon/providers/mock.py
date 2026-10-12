"""Offline provider: replays scripted or cached responses. Used for demos and tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from recon.providers.base import BadOutput, ProviderError


class MockProvider:
    """Returns responses from a script, a function, or JSON files named by image sha256."""

    name = "mock"

    def __init__(self, script: list[str | ProviderError] | None = None,
                 fn: Callable[[bytes, str], str] | None = None,
                 fixture_dir: Path | None = None):
        self.script = list(script or [])
        self.fn = fn
        self.fixture_dir = fixture_dir
        self.calls: list[dict] = []

    def extract(self, *, image_jpeg: bytes, prompt: str, schema: dict | None,
                api_key: str, model: str, timeout: float) -> str:
        """Return the next scripted response (or raise it if it is an error)."""
        self.calls.append({"prompt": prompt[:40], "model": model})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, ProviderError):
                raise item
            return item
        if self.fn:
            return self.fn(image_jpeg, prompt)
        if self.fixture_dir:
            p = self.fixture_dir / f"{hashlib.sha256(image_jpeg).hexdigest()}.json"
            if p.exists():
                return json.dumps(json.loads(p.read_text())["pass_a"])
        raise BadOutput(self.name, "no cached extraction for this page")

    def test_connection(self, *, api_key: str, model: str, timeout: float) -> str:
        """Always ok."""
        return "ok"

    def list_models(self, *, api_key: str, timeout: float) -> list[str]:
        """Single pseudo-model."""
        return ["cached"]
