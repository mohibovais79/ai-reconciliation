"""Review queue (doubtful items only), decision fingerprints, applying stored decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from recon.config import Settings
from recon.ingest import SourceContext, finish_units, set_field
from recon.match import MatchResult, _display
from recon.models import Flag, FlagType, SourceRow
from recon.store import Store

QUESTIONS = {
    FlagType.UNCLEAR_FIELD: "What does this say?",
    FlagType.STRUCK_VALUE: "A value was crossed out. Which is final?",
    FlagType.PASS_DISAGREE: "Two readings differ. Which is right?",
    FlagType.FUZZY_MATCH_PROPOSAL: "Are these the same receipt?",
    FlagType.CONFLICTING_DUPLICATE: "Same key, different content. Which is right?",
    FlagType.NON_RECEIPT_LINK: "This is a return/adjustment. Link to which receipt?",
    FlagType.SUSPECT_ENTRY: "Late/backdated entry not in other sources. Legitimate?",
    FlagType.UNIT_INCOMPARABLE: "Units can't be compared. Enter the quantity in the other "
                                "unit, or mark as exception.",
    FlagType.ROW_COUNT_MISMATCH: "Rows may be missing from this page. Check the image.",
    FlagType.BAD_FORMAT: "This value is not in a usable format. What should it be?",
}
ACTIONS = {"accept_value", "enter_value", "confirm_match", "reject_match", "mark_duplicate",
           "link_non_receipt", "accept_as_exception", "defer"}
VALUE_ACTIONS = {"accept_value", "enter_value"}


@dataclass
class ReviewItem:
    """One question for a human."""

    fingerprint: str
    type: FlagType
    source: str
    row_id: str
    field: str | None
    question: str
    message: str
    suggestion: str | None = None
    candidates: list[str] = field(default_factory=list)
    hints: dict[str, str] = field(default_factory=dict)   # other sources' values (hints only)
    page: int | None = None
    impact: float = 0.0
    blocking: bool = True
    score: float | None = None


def row_hash(row: SourceRow) -> str:
    """Hash of the row's original content; changes when the underlying data changes."""
    return hashlib.sha256(json.dumps(row.raw, sort_keys=True, default=str).encode()).hexdigest()


def fingerprint(source: str, content_hash: str, issue: str, fieldname: str | None) -> str:
    """Decision key = hash(source, content hash, issue type, field)."""
    return hashlib.sha256(f"{source}|{content_hash}|{issue}|{fieldname}".encode()).hexdigest()[:24]


def row_fp(row: SourceRow, issue: str, fieldname: str | None) -> str:
    """Fingerprint for a row-level item."""
    return fingerprint(row.source, row_hash(row), issue, fieldname)


def apply_decisions(rows: list[SourceRow], store: Store, ctxs: dict[str, SourceContext],
                    settings: Settings, learned: dict | None = None
                    ) -> tuple[dict[str, str], set[tuple[str, str]], int]:
    """Apply valid stored decisions before matching. Returns (confirmed, rejected, n_stale)."""
    confirmed: dict[str, str] = {}
    rejected: set[tuple[str, str]] = set()
    stale = 0
    for r in rows:
        decided = r.extras.setdefault("decided", [])
        for d in store.decisions_for_row(r.row_id):
            if row_fp(r, d["issue_type"], d["field"]) != d["fingerprint"]:
                stale += 1          # data changed since the decision: ask again
                continue
            act, val = d["action"], d["value"]
            if act == "defer":
                continue
            if act in VALUE_ACTIONS and d["field"]:
                set_field(r, d["field"], val, ctxs[r.source], settings, learned)
                finish_units(r, ctxs[r.source], settings, learned)
                r.extras.setdefault("decided_fields", []).append(d["field"])
            elif act == "confirm_match" and val:
                confirmed[r.row_id] = val
            elif act == "reject_match" and val:
                rejected.add((r.row_id, val))
            elif act == "mark_duplicate":
                r.extras["force_duplicate"] = True
            elif act == "link_non_receipt":
                r.extras["linked_to"] = val
            decided.append([d["issue_type"], d["field"]])
    return confirmed, rejected, stale


def mark_resolved(rows: list[SourceRow]) -> None:
    """Resolve flags that a stored decision already answered."""
    for r in rows:
        decided = {(t, f) for t, f in r.extras.get("decided", [])}
        fields = set(r.extras.get("decided_fields", []))
        for fl in r.flags:
            if (fl.type.value, fl.field) in decided or (fl.field and fl.field in fields
                                                         and fl.type != FlagType.BAD_FORMAT):
                fl.resolved = True
            if fl.type == FlagType.BAD_FORMAT and fl.field in fields:
                fl.resolved = True


def _impact(fl: Flag, row: SourceRow) -> float:
    base = {FlagType.UNCLEAR_FIELD: 3, FlagType.STRUCK_VALUE: 3, FlagType.PASS_DISAGREE: 3,
            FlagType.CONFLICTING_DUPLICATE: 2.5, FlagType.UNIT_INCOMPARABLE: 2.5,
            FlagType.SUSPECT_ENTRY: 2, FlagType.FUZZY_MATCH_PROPOSAL: 2}.get(fl.type, 1)
    if fl.field == "qty":
        base += 2
    q = abs(row.qty_base or 0)
    return float(base) + min(float(q), 1e6) / 1e6


def build_queue(rows: list[SourceRow], mr: MatchResult, page_flags: list[tuple[int, Flag]],
                page_sha: dict[int, str]) -> list[ReviewItem]:
    """Every unresolved doubt as a card, highest impact first. Plain mismatches are not here."""
    groups = {g.group_id: g for g in mr.groups}
    items: list[ReviewItem] = []
    for r in rows:
        g = groups.get(mr.group_of.get(r.row_id, ""))
        for fl in r.flags:
            if fl.resolved or not (fl.blocking or fl.type == FlagType.NON_RECEIPT_LINK):
                continue
            hints = {}
            if g and fl.field:
                hints = {s: _display(g.primary(s), fl.field) for s in g.sources if s != r.source}
            items.append(ReviewItem(
                fingerprint=row_fp(r, fl.type.value, fl.field), type=fl.type, source=r.source,
                row_id=r.row_id, field=fl.field, question=QUESTIONS.get(fl.type, "Please check"),
                message=fl.message, suggestion=fl.suggestion, candidates=list(fl.candidates),
                hints=hints, page=r.extras.get("page"), impact=_impact(fl, r),
                blocking=fl.blocking, score=fl.score))
    for p, fl in page_flags:
        items.append(ReviewItem(
            fingerprint=fingerprint("REG", page_sha.get(p, str(p)), fl.type.value, None),
            type=fl.type, source="REG", row_id=f"REG:page{p}", field=None,
            question=QUESTIONS.get(fl.type, "Please check this page"), message=fl.message,
            page=p, impact=2.5, blocking=fl.blocking))
    seen, out = set(), []
    for it in sorted(items, key=lambda i: -i.impact):
        if it.fingerprint not in seen:
            seen.add(it.fingerprint)
            out.append(it)
    return out


def record_decision(store: Store, item: ReviewItem, action: str, value: str | None,
                    reviewer: str, original: str | None = None) -> None:
    """Persist a decision (audit: who, when, original value). Never deletes rows."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    store.put_decision({"fingerprint": item.fingerprint, "source": item.source,
                        "row_id": item.row_id, "issue_type": item.type.value,
                        "field": item.field, "action": action, "value": value,
                        "original_value": original if original is not None else item.message,
                        "reviewer": reviewer or "unknown"})


def strong_proposals(items: list[ReviewItem], min_score: float) -> list[ReviewItem]:
    """Match proposals strong enough to offer a one-click 'confirm all' (still a human act)."""
    return [i for i in items if i.type == FlagType.FUZZY_MATCH_PROPOSAL
            and (i.score or 0) >= min_score]


def resolved_page_items(store: Store, items: list[ReviewItem]) -> set[str]:
    """Fingerprints of page-level items already decided (anything except defer)."""
    out = set()
    for it in items:
        d = store.get_decision(it.fingerprint)
        if d and d["action"] != "defer":
            out.add(it.fingerprint)
    return out
