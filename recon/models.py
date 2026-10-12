"""Core pydantic models shared across the pipeline."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["REG", "ERP", "STORE"]


class Cell(BaseModel):
    """One register cell exactly as the model read it."""

    text: str | None = None
    legibility: Literal["clear", "uncertain", "illegible"] = "clear"
    struck_text: str | None = None
    note: str | None = None


class RegisterPageExtraction(BaseModel):
    """Structured transcription of one register page (one pass)."""

    page_label: str | None = None
    period_label: str | None = None
    columns: list[str]
    rows: list[dict[str, Cell]]
    rows_visible: int | None = None
    provider: str = ""
    model: str = ""
    pass_id: str = "A"


class FlagType(StrEnum):
    UNCLEAR_FIELD = "UNCLEAR_FIELD"
    STRUCK_VALUE = "STRUCK_VALUE"
    PASS_DISAGREE = "PASS_DISAGREE"
    ROW_COUNT_MISMATCH = "ROW_COUNT_MISMATCH"
    BAD_FORMAT = "BAD_FORMAT"
    SEQUENCE_ANOMALY = "SEQUENCE_ANOMALY"
    TRANSPOSITION_HINT = "TRANSPOSITION_HINT"
    AMBIGUOUS_DATE = "AMBIGUOUS_DATE"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    UNIT_INCOMPARABLE = "UNIT_INCOMPARABLE"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"
    FUZZY_MATCH_PROPOSAL = "FUZZY_MATCH_PROPOSAL"
    NON_RECEIPT_LINK = "NON_RECEIPT_LINK"
    SUSPECT_ENTRY = "SUSPECT_ENTRY"


# Flags that block a row (send it to NEEDS_REVIEW) until a human decides.
BLOCKING_FLAGS = {
    FlagType.UNCLEAR_FIELD,
    FlagType.STRUCK_VALUE,
    FlagType.PASS_DISAGREE,
    FlagType.CONFLICTING_DUPLICATE,
    FlagType.FUZZY_MATCH_PROPOSAL,
    FlagType.SUSPECT_ENTRY,
    FlagType.UNIT_INCOMPARABLE,
}


class Flag(BaseModel):
    """A doubt raised by deterministic code about a row or field."""

    type: FlagType
    field: str | None = None
    message: str = ""
    suggestion: str | None = None
    candidates: list[str] = []          # e.g. row_ids for a proposed match
    blocking: bool = True
    resolved: bool = False
    score: float | None = None          # Tier-2 match score, when relevant


class Outcome(StrEnum):
    NON_RECEIPT = "NON_RECEIPT"
    DUPLICATE_EXTRA = "DUPLICATE_EXTRA"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    MISSING = "MISSING"
    MISMATCHED = "MISMATCHED"
    MATCHED = "MATCHED"


OUTCOME_ORDER = list(Outcome)


class SourceRow(BaseModel):
    """Canonical row after column mapping and normalisation."""

    source: Source
    row_id: str
    raw: dict[str, Any] = {}
    key: str | None = None
    key_raw: str | None = None
    date: dt.date | None = None
    date_raw: str | None = None
    date_was_text: bool = False
    supplier_raw: str | None = None
    supplier_norm: str | None = None
    item_raw: str | None = None
    item_norm: str | None = None
    lot: str | None = None
    qty_raw: str | float | None = None
    qty: Decimal | None = None
    unit_raw: str | None = None
    unit: str | None = None
    qty_base: Decimal | None = None
    base_unit: str | None = None
    flags: list[Flag] = []
    extras: dict[str, Any] = {}
    entered_at: dt.datetime | None = None

    def blocking_flags(self) -> list[Flag]:
        """Unresolved flags that need a human decision."""
        return [f for f in self.flags if f.blocking and not f.resolved]


class DroppedRow(BaseModel):
    """A file row removed at ingest (blank, repeated header, totals) with the reason."""

    source: Source
    row_ref: str                       # "r17" for files, "p2r5" for register pages
    reason: str
    raw: dict[str, Any] = {}


class LedgerEntry(BaseModel):
    """Exactly one outcome per source row."""

    row_id: str
    source: Source
    outcome: Outcome
    group_id: str | None = None
    key: str | None = None
    qty_base: Decimal | None = None
    base_unit: str | None = None
    missing_from: list[str] = []
    mismatched_fields: list[str] = []
    detail: str = ""
    hints: list[str] = []


class InputFile(BaseModel):
    """Provenance for one uploaded input."""

    source: Source
    name: str
    sha256: str
    rows_read: int
    rows_ingested: int
    rows_dropped: int
    note: str = ""


class PageResult(BaseModel):
    """Result of extracting one register page."""

    page_index: int
    file_name: str
    sha256: str
    status: Literal["ok", "failed"]
    provider: str = ""
    model: str = ""
    cached: bool = False
    error: str | None = None
    extraction: RegisterPageExtraction | None = None
    verify: RegisterPageExtraction | None = None
    flags_by_row: dict[int, list[Flag]] = Field(default_factory=dict)
    page_flags: list[Flag] = Field(default_factory=list)
