"""Logging setup with a redaction filter so secrets never reach log output."""

from __future__ import annotations

import hashlib
import logging
import re
import threading

# Common API-key shapes (Google, OpenAI-style, Groq, OpenRouter, bearer tokens, Telegram bots).
KEY_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk-or-v1-[0-9A-Za-z]{20,}"),
    re.compile(r"sk-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gsk_[0-9A-Za-z]{20,}"),
    re.compile(r"\b\d{6,12}:[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"(?i)(bearer\s+)[0-9A-Za-z._\-]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-goog-api-key|authorization|token)\s*[=:]\s*)[^\s&\"']{8,}"),
]
MASK = "[REDACTED]"

# We remember only sha256 digests (by length) of keys seen in-process, never the keys themselves.
_seen: dict[int, set[str]] = {}
_lock = threading.Lock()


def _digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()


def register_secret(value: str | None) -> None:
    """Remember a secret's digest so exact occurrences get masked."""
    if not value or len(value) < 8:
        return
    with _lock:
        _seen.setdefault(len(value), set()).add(_digest(value))


def _mask_seen(text: str) -> str:
    """Mask any substring whose digest matches a registered secret."""
    with _lock:
        items = [(n, set(d)) for n, d in _seen.items()]
    for n, digests in items:
        i = 0
        while i + n <= len(text):
            if _digest(text[i : i + n]) in digests:
                text = text[:i] + MASK + text[i + n :]
                i += len(MASK)
            else:
                i += 1
    return text


def redact(text: object) -> str:
    """Return `text` with key-like strings and registered secrets masked."""
    s = str(text)
    for pat in KEY_PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: m.group(1) + MASK, s)
        else:
            s = pat.sub(MASK, s)
    return _mask_seen(s)


class RedactionFilter(logging.Filter):
    """Logging filter that rewrites every record through `redact`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_redaction(handler: logging.Handler) -> logging.Handler:
    """Attach the redaction filter to a handler (idempotent)."""
    if not any(isinstance(f, RedactionFilter) for f in handler.filters):
        handler.addFilter(RedactionFilter())
    return handler


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging with redaction on every handler; silence chatty HTTP libs."""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    for h in root.handlers:
        install_redaction(h)
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep them quiet.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
