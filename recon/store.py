"""SQLite persistence: page cache, decisions, mapping profiles, aliases, runs. Never keys."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS page_cache (
    cache_key TEXT PRIMARY KEY, image_sha TEXT, provider TEXT, model TEXT,
    prompt_version TEXT, pass_id TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    fingerprint TEXT PRIMARY KEY, source TEXT, row_id TEXT, issue_type TEXT, field TEXT,
    action TEXT, value TEXT, original_value TEXT, reviewer TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS mapping_profiles (
    fingerprint TEXT PRIMARY KEY, source TEXT, mapping TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS aliases (
    kind TEXT, raw TEXT, canonical TEXT, PRIMARY KEY (kind, raw));
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS notifications (
    msg_hash TEXT, run_id TEXT, channel TEXT, sent_at TEXT, PRIMARY KEY (msg_hash, run_id));
"""


def now_iso() -> str:
    """Current UTC time as ISO-8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """Thin wrapper over a SQLite file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()

    # page cache
    def get_page(self, cache_key: str) -> dict | None:
        """Return a cached extraction payload and its timestamp, if any."""
        r = self.conn.execute(
            "SELECT payload, created_at FROM page_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return {"payload": json.loads(r[0]), "created_at": r[1]} if r else None

    def put_page(self, cache_key: str, image_sha: str, provider: str, model: str,
                 prompt_version: str, pass_id: str, payload: dict) -> None:
        """Cache one page extraction."""
        self.conn.execute(
            "INSERT OR REPLACE INTO page_cache VALUES (?,?,?,?,?,?,?,?)",
            (cache_key, image_sha, provider, model, prompt_version, pass_id,
             json.dumps(payload), now_iso()),
        )
        self.conn.commit()

    def latest_page_by_image(self, image_sha: str, pass_id: str = "A") -> dict | None:
        """Most recent cached extraction for an image, from any provider (offline mode)."""
        r = self.conn.execute(
            "SELECT payload, created_at, provider, model FROM page_cache "
            "WHERE image_sha=? AND pass_id=? ORDER BY created_at DESC LIMIT 1",
            (image_sha, pass_id),
        ).fetchone()
        if not r:
            return None
        return {"payload": json.loads(r[0]), "created_at": r[1], "provider": r[2], "model": r[3]}

    # decisions
    def get_decision(self, fingerprint: str) -> dict | None:
        """Return a stored review decision."""
        cur = self.conn.execute("SELECT * FROM decisions WHERE fingerprint=?", (fingerprint,))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r, strict=True)) if r else None

    def put_decision(self, d: dict[str, Any]) -> None:
        """Insert or replace a review decision."""
        cols = ["fingerprint", "source", "row_id", "issue_type", "field", "action", "value",
                "original_value", "reviewer", "decided_at"]
        vals = [d.get(c) for c in cols]
        if vals[-1] is None:
            vals[-1] = now_iso()
        self.conn.execute(
            f"INSERT OR REPLACE INTO decisions ({','.join(cols)}) VALUES "
            f"({','.join('?' * len(cols))})",
            vals,
        )
        self.conn.commit()

    def decisions_for_row(self, row_id: str) -> list[dict]:
        """Decisions recorded against a row id (validated by fingerprint by the caller)."""
        cur = self.conn.execute("SELECT * FROM decisions WHERE row_id=?", (row_id,))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def all_decisions(self) -> list[dict]:
        """All decisions, newest first (audit log)."""
        cur = self.conn.execute("SELECT * FROM decisions ORDER BY decided_at DESC")
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    # mapping profiles
    def get_mapping(self, fingerprint: str) -> dict | None:
        """Saved column mapping for a header fingerprint."""
        r = self.conn.execute(
            "SELECT mapping FROM mapping_profiles WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return json.loads(r[0]) if r else None

    def put_mapping(self, fingerprint: str, source: str, mapping: dict) -> None:
        """Save a confirmed column mapping."""
        self.conn.execute(
            "INSERT OR REPLACE INTO mapping_profiles VALUES (?,?,?,?)",
            (fingerprint, source, json.dumps(mapping), now_iso()),
        )
        self.conn.commit()

    # aliases
    def aliases(self, kind: str) -> dict[str, str]:
        """Learned aliases of a kind (e.g. 'supplier', 'unit')."""
        rows = self.conn.execute("SELECT raw, canonical FROM aliases WHERE kind=?", (kind,))
        return dict(rows.fetchall())

    def put_alias(self, kind: str, raw: str, canonical: str) -> None:
        """Learn an alias from a review decision."""
        self.conn.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?)", (kind, raw, canonical))
        self.conn.commit()

    # runs + notifications
    def record_run(self, run_id: str, status: str, summary: dict) -> None:
        """Record a run in history."""
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
            (run_id, now_iso(), status, json.dumps(summary, default=str)),
        )
        self.conn.commit()

    def notification_sent(self, msg_hash: str, run_id: str) -> bool:
        """True if this exact message was already sent for this run."""
        r = self.conn.execute(
            "SELECT 1 FROM notifications WHERE msg_hash=? AND run_id=?", (msg_hash, run_id)
        ).fetchone()
        return r is not None

    def record_notification(self, msg_hash: str, run_id: str, channel: str) -> None:
        """Remember a sent notification to prevent duplicates."""
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications VALUES (?,?,?,?)",
            (msg_hash, run_id, channel, now_iso()),
        )
        self.conn.commit()
