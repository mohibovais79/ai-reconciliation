"""Deterministic checks on canonical rows (doubts raised by code, not model confidence)."""

from __future__ import annotations

import re
from collections import Counter

from recon.models import Flag, FlagType, SourceRow


def _shape(s: str) -> str:
    """Pattern of a value: letters -> A, digits -> 9, runs collapsed."""
    return re.sub(r"(.)\1+", r"\1", re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "A", s)))


def check_sequence(rows: list[SourceRow]) -> None:
    """Register keys should rise down a page; flag rows that break the order (hint only)."""
    by_page: dict[object, list[SourceRow]] = {}
    for r in rows:
        if r.source == "REG" and r.key and r.key.isdigit():
            by_page.setdefault(r.extras.get("page"), []).append(r)
    for page_rows in by_page.values():
        for prev, cur in zip(page_rows, page_rows[1:], strict=False):
            if int(cur.key) <= int(prev.key):
                cur.flags.append(Flag(type=FlagType.SEQUENCE_ANOMALY, field="key", blocking=False,
                                      message=f"key {cur.key} comes after {prev.key} on the page"))


def check_lot_shapes(rows: list[SourceRow]) -> None:
    """Flag lots whose shape differs from the dominant shape in the other sources (hint)."""
    for src in {r.source for r in rows}:
        others = Counter(_shape(r.extras.get("lot_raw") or "") for r in rows
                         if r.source != src and r.extras.get("lot_raw"))
        if not others:
            continue
        dominant, n = others.most_common(1)[0]
        if n < 0.6 * sum(others.values()):
            continue
        for r in rows:
            raw = r.extras.get("lot_raw")
            if r.source == src and raw and _shape(raw) != dominant:
                r.flags.append(Flag(type=FlagType.BAD_FORMAT, field="lot", blocking=False,
                                    message=f"lot '{raw}' does not look like the others"))


def run_checks(rows: list[SourceRow]) -> None:
    """Apply all row-level deterministic checks."""
    check_sequence(rows)
    check_lot_shapes(rows)
