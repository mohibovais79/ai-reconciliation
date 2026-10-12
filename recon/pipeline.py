"""One reconciliation run: mapped tables -> rows -> decisions -> matching -> ledger -> proof."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from recon.config import Settings
from recon.ingest import SourceContext, Table, to_source_rows
from recon.ledger import build_ledger
from recon.match import MatchResult, build_groups, compare_group, flag_suspect_entries
from recon.models import DroppedRow, InputFile, LedgerEntry, PageResult, SourceRow
from recon.proof import ProofResult, check
from recon.review import ReviewItem, apply_decisions, build_queue, mark_resolved
from recon.store import Store
from recon.validate import run_checks


@dataclass
class RunResult:
    """Everything the UI and report need from one run."""

    run_id: str
    status: str
    rows: list[SourceRow]
    dropped: list[DroppedRow]
    ledger: list[LedgerEntry]
    match: MatchResult
    proof: ProofResult
    queue: list[ReviewItem]
    inputs: list[InputFile]
    pages: list[PageResult] = field(default_factory=list)
    contexts: dict[str, SourceContext] = field(default_factory=dict)
    stale_decisions: int = 0
    period: str = ""

    @property
    def complete(self) -> bool:
        """True only when every page was read and the proof ties."""
        return self.status.startswith("COMPLETE")

    @property
    def awaiting(self) -> int:
        """Doubtful items still waiting for a human."""
        return sum(1 for i in self.queue if i.blocking)


def run_status(pages: list[PageResult], proof_ok: bool, awaiting: int) -> str:
    """COMPLETE / COMPLETE - N ITEMS AWAITING REVIEW / INCOMPLETE - ... / FAILED - ..."""
    if not proof_ok:
        return "FAILED - completeness proof did not tie"
    failed = [p for p in pages if p.status == "failed"]
    if failed:
        names = ", ".join(f"page {p.page_index + 1} ({p.file_name})" for p in failed)
        return f"INCOMPLETE - {names} extraction failed"
    return f"COMPLETE - {awaiting} ITEMS AWAITING REVIEW" if awaiting else "COMPLETE"


def reconcile(tables: dict[str, Table], mappings: dict[str, dict[str, str | None]],
              settings: Settings, store: Store, pages: list[PageResult] | None = None,
              period_year: int | None = None) -> RunResult:
    """Run the deterministic pipeline. Never raises on data problems; status says what's wrong."""
    pages = pages or []
    learned = {"supplier": store.aliases("supplier"), "unit": store.aliases("unit")}
    rows: list[SourceRow] = []
    dropped: list[DroppedRow] = []
    ctxs: dict[str, SourceContext] = {}
    for src, t in tables.items():
        r, ctxs[src] = to_source_rows(t, mappings[src], settings, period_year, learned)
        rows += r
        dropped += t.dropped
    run_checks(rows)
    confirmed, rejected, stale = apply_decisions(rows, store, ctxs, settings, learned)
    mr = build_groups(rows, settings, confirmed, rejected)
    for g in mr.groups:
        compare_group(g, settings)
    flag_suspect_entries(mr.groups, rows, settings)
    mr.duplicates |= {r.row_id for r in rows if r.extras.get("force_duplicate")}
    mark_resolved(rows)
    ledger = build_ledger(rows, mr, list(tables))
    proof = check({s: t.rows_read for s, t in tables.items()}, rows, dropped, ledger)
    queue = _queue(rows, mr, pages, store)
    inputs = [InputFile(source=s, name=t.name, sha256=t.sha256, rows_read=t.rows_read,
                        rows_ingested=len(t.records), rows_dropped=len(t.dropped))
              for s, t in tables.items()]
    status = run_status(pages, proof.ok, sum(1 for i in queue if i.blocking))
    run = RunResult(uuid.uuid4().hex[:12], status, rows, dropped, ledger, mr, proof, queue,
                    inputs, pages, ctxs, stale, _period(tables, period_year))
    store.record_run(run.run_id, status, {"rows": len(rows), "awaiting": run.awaiting})
    return run


def _period(tables: dict[str, Table], period_year: int | None) -> str:
    """Period label as written on the register, else the user-set year."""
    labels = [p for t in tables.values() for p in t.meta.get("periods", []) if p]
    return labels[0] if labels else (str(period_year) if period_year else "")


def _queue(rows: list[SourceRow], mr: MatchResult, pages: list[PageResult],
           store: Store) -> list[ReviewItem]:
    """Review queue minus page-level items a reviewer already settled."""
    page_flags = [(p.page_index + 1, f) for p in pages for f in p.page_flags if f.blocking]
    q = build_queue(rows, mr, page_flags, {p.page_index + 1: p.sha256 for p in pages})
    done = {it.fingerprint for it in q if it.row_id.startswith("REG:page")
            and (d := store.get_decision(it.fingerprint)) and d["action"] != "defer"}
    return [it for it in q if it.fingerprint not in done]
