"""Optional Telegram sender. Token passed per request, never stored; no duplicate sends."""

from __future__ import annotations

import hashlib

import httpx

from recon.logging_utils import redact, register_secret
from recon.store import Store

API = "https://api.telegram.org"


class NotifyError(RuntimeError):
    """Sending failed after retries (message stays downloadable in the UI)."""


class AlreadySent(RuntimeError):
    """This exact message was already sent for this run."""


def message_hash(text: str) -> str:
    """Stable hash used to refuse duplicate sends."""
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def send_telegram(text: str, *, token: str, chat_id: str, run_id: str, store: Store,
                  force: bool = False, attempts: int = 3,
                  transport: httpx.BaseTransport | None = None) -> None:
    """POST sendMessage; retry transient failures; record hash+run so it isn't resent."""
    register_secret(token)
    h = message_hash(text)
    if store.notification_sent(h, run_id) and not force:
        raise AlreadySent("this summary was already sent for this run")
    last = ""
    for _ in range(attempts):
        try:
            with httpx.Client(timeout=20, transport=transport) as c:
                r = c.post(f"{API}/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text})
            if r.status_code == 200:
                store.record_notification(h, run_id, "telegram")
                return
            last = f"Telegram answered HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404):
                break
        except httpx.HTTPError as e:
            last = f"network error ({type(e).__name__})"
    raise NotifyError(redact(f"could not send to Telegram: {last}"))
