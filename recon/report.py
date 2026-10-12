"""Excel workbook (live-formula Proof tab), CSV exports and the short controller summary."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import Counter
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from recon.config import Settings
from recon.models import Outcome
from recon.pipeline import RunResult

RED, GREEN, AMBER = "FFC7CE", "C6EFCE", "FFEB9C"
LEDGER_COLS = ["row_id", "source", "outcome", "group", "key", "date", "supplier", "item",
               "lot", "qty_raw", "unit_raw", "qty_base", "base_unit", "missing_from",
               "mismatched_fields", "detail", "hints"]


def sanitize(v: object) -> object:
    """Neutralise formula injection from uploaded data."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _put(ws, row: list) -> None:
    ws.append([sanitize(v) for v in row])


def _header(ws, cols: list[str]) -> None:
    ws.append(cols)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.freeze_panes = f"A{ws.max_row + 1}"


def _widths(ws) -> None:
    for i, col in enumerate(ws.columns, 1):
        n = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, n + 2), 60)


def ledger_rows(run: RunResult) -> list[list]:
    """One row per source row: identity, raw values, outcome and why."""
    by_id = {r.row_id: r for r in run.rows}
    out = []
    for e in run.ledger:
        r = by_id[e.row_id]
        out.append([e.row_id, e.source, e.outcome.value, e.group_id, r.key_raw,
                    r.date.isoformat() if r.date else r.date_raw, r.supplier_raw, r.item_raw,
                    r.extras.get("lot_raw"), None if r.qty_raw is None else str(r.qty_raw),
                    r.unit_raw or r.unit, r.qty_base, r.base_unit, ", ".join(e.missing_from),
                    ", ".join(e.mismatched_fields), e.detail, " | ".join(e.hints)])
    return out


def action_items(run: RunResult, settings: Settings) -> list[tuple[str, str, str]]:
    """(owner, receipt, what to fix) from mismatches and missing rows."""
    owners, labels = settings.sources.owners, settings.sources.labels
    items = []
    for g in run.match.groups:
        ref = _ref(g)
        for m in g.mismatches:
            for src in m.outliers or g.sources:
                items.append((owners.get(src, src), ref,
                              f"check {m.field}: {labels.get(src, src)} has {m.values[src]} "
                              f"({m.hint})"))
        present = set(g.sources)
        if len(present) < len(run.inputs) and not any(
                e.outcome == Outcome.NEEDS_REVIEW for e in run.ledger if e.group_id == g.group_id):
            for inp in run.inputs:
                if inp.source not in present:
                    items.append((owners.get(inp.source, inp.source), ref,
                                  f"not recorded in {labels.get(inp.source, inp.source)}"))
    return items


def _ref(g) -> str:
    if g.key:
        return f"#{g.key}"
    r = g.primary(g.sources[0])
    return f"{r.row_id} {r.item_raw or ''}".strip()


def build_workbook(run: RunResult, settings: Settings, decisions: list[dict]) -> bytes:
    """All report tabs; every string cell is sanitised."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    _summary_tab(ws, run, settings)
    _actions_tab(wb.create_sheet("Action lists"), run, settings)
    _mismatch_tab(wb.create_sheet("Mismatched"), run, settings)
    _filtered(wb.create_sheet("Missing"), run, {Outcome.MISSING})
    _filtered(wb.create_sheet("Duplicates & non-receipts"), run,
              {Outcome.DUPLICATE_EXTRA, Outcome.NON_RECEIPT})
    _filtered(wb.create_sheet("Matched"), run, {Outcome.MATCHED})
    _review_tab(wb.create_sheet("Review log"), run, decisions)
    ledger = wb.create_sheet("Ledger")
    _header(ledger, LEDGER_COLS)
    for row in ledger_rows(run):
        _put(ledger, row)
    _proof_tab(wb.create_sheet("Proof"), run)
    _inputs_tab(wb.create_sheet("Inputs"), run)
    for sh in wb.worksheets:
        _widths(sh)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _summary_tab(ws, run: RunResult, settings: Settings) -> None:
    ws.append([f"RUN STATUS: {run.status}"])
    fill = GREEN if run.status == "COMPLETE" else AMBER if run.complete else RED
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].fill = PatternFill("solid", fgColor=fill)
    ws.append([])
    for line in summary_text(run, settings, "").splitlines():
        _put(ws, [line])
    ws.append([])
    _header(ws, ["Source", *[o.value for o in Outcome], "Total"])
    for inp in run.inputs:
        c = Counter(e.outcome for e in run.ledger if e.source == inp.source)
        _put(ws, [settings.sources.labels.get(inp.source, inp.source),
                  *[c.get(o, 0) for o in Outcome], sum(c.values())])
    ws.freeze_panes = None


def _actions_tab(ws, run: RunResult, settings: Settings) -> None:
    _header(ws, ["Owner", "Receipt", "What to fix"])
    for item in sorted(action_items(run, settings)):
        _put(ws, list(item))


def _mismatch_tab(ws, run: RunResult, settings: Settings) -> None:
    srcs = [i.source for i in run.inputs]
    _header(ws, ["Receipt", "Field", *[settings.sources.labels.get(s, s) for s in srcs],
                 "Hint", "Row ids"])
    for g in run.match.groups:
        ids = ", ".join(r.row_id for rs in g.members.values() for r in rs)
        for m in g.mismatches:
            _put(ws, [_ref(g), m.field, *[m.values.get(s, "") for s in srcs], m.hint, ids])


def _filtered(ws, run: RunResult, outcomes: set[Outcome]) -> None:
    _header(ws, LEDGER_COLS)
    for row in ledger_rows(run):
        if row[2] in outcomes:
            _put(ws, row)


def _review_tab(ws, run: RunResult, decisions: list[dict]) -> None:
    _header(ws, ["Status", "When", "Reviewer", "Row", "Issue", "Field", "Action", "Value",
                 "Original / question"])
    for d in decisions:
        _put(ws, ["decided", d["decided_at"], d["reviewer"], d["row_id"], d["issue_type"],
                  d["field"], d["action"], d["value"], d["original_value"]])
    for q in run.queue:
        _put(ws, ["awaiting review", "", "", q.row_id, q.type.value, q.field, "", "",
                  f"{q.question} {q.message}"])


def _proof_tab(ws, run: RunResult) -> None:
    ws.append([f"Python proof: {'ALL CHECKS PASS' if run.proof.ok else 'FAILED'}"])
    ws["A1"].fill = PatternFill("solid", fgColor=GREEN if run.proof.ok else RED)
    ws.append(["Live formulas below recount the Ledger tab; Status turns red if anything differs."])
    ws.append([])
    _header(ws, ["Source", "Rows read (file)", "Dropped at ingest", "Rows ingested",
                 "Ledger rows (formula)", "Unique outcomes sum (formula)", "Status"])
    outs = [o.value for o in Outcome]
    for inp in run.inputs:
        r = ws.max_row + 1
        s = f'"{inp.source}"'
        per = "+".join(f'COUNTIFS(Ledger!B:B,{s},Ledger!C:C,"{o}")' for o in outs)
        ws.append([inp.source, inp.rows_read, inp.rows_dropped, inp.rows_ingested,
                   f"=COUNTIFS(Ledger!B:B,{s})", f"={per}",
                   f'=IF(AND(B{r}=C{r}+D{r},E{r}=D{r},F{r}=D{r}),"OK","FAIL")'])
    ws.append([])
    _header(ws, ["Source", "Base unit", "Qty total (Python, input rows)", "Qty total (formula)",
                 "Status"])
    for src, units in run.proof.qty_totals.items():
        for unit, total in units.items():
            r = ws.max_row + 1
            crit = 'Ledger!M:M,""' if unit == "(no unit)" else f'Ledger!M:M,"{unit}"'
            ws.append([src, unit, total, f'=SUMIFS(Ledger!L:L,Ledger!B:B,"{src}",{crit})',
                       f'=IF(ABS(C{r}-D{r})<0.000001,"OK","FAIL")'])
    ws.append([])
    _header(ws, ["Check", "Source", "Expected", "Actual", "OK"])
    for c in run.proof.checks:
        _put(ws, [c.name, c.source, c.expected, c.actual, "OK" if c.ok else "FAIL"])
    rng = f"A1:G{ws.max_row}"
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"OK"'],
                                                  fill=PatternFill("solid", fgColor=GREEN)))
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"FAIL"'],
                                                  fill=PatternFill("solid", fgColor=RED)))


def _inputs_tab(ws, run: RunResult) -> None:
    ws.append([f"Run {run.run_id} at {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC"])
    _header(ws, ["Source", "File", "SHA-256", "Rows read", "Rows ingested", "Rows dropped"])
    for i in run.inputs:
        _put(ws, [i.source, i.name, i.sha256, i.rows_read, i.rows_ingested, i.rows_dropped])
    ws.append([])
    _header(ws, ["Register page", "File", "SHA-256", "Status", "Read by", "Model", "Cached",
                 "Error"])
    for p in run.pages:
        _put(ws, [p.page_index + 1, p.file_name, p.sha256, p.status, p.provider, p.model,
                  "yes" if p.cached else "no", p.error or ""])
    ws.append([])
    _header(ws, ["Dropped row", "Source", "Reason"])
    for d in run.dropped:
        _put(ws, [d.row_ref, d.source, d.reason])


def to_csv(header: list[str], rows: list[list]) -> bytes:
    """CSV bytes with the same injection sanitising as the workbook."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([sanitize("" if v is None else v) for v in r])
    return buf.getvalue().encode("utf-8-sig")


def ledger_csv(run: RunResult) -> bytes:
    """Ledger export."""
    return to_csv(LEDGER_COLS, ledger_rows(run))


def mismatched_csv(run: RunResult, settings: Settings) -> bytes:
    """Mismatched-fields export."""
    srcs = [i.source for i in run.inputs]
    rows = [[_ref(g), m.field, *[m.values.get(s, "") for s in srcs], m.hint]
            for g in run.match.groups for m in g.mismatches]
    return to_csv(["receipt", "field", *srcs, "hint"], rows)


# ---------- summary ----------

def _exception_lines(run: RunResult, settings: Settings) -> list[str]:
    labels = settings.sources.labels
    lines = []
    for g in run.match.groups:
        for m in g.mismatches:
            agree = [s for s in m.values if s not in m.outliers]
            if m.outliers and agree:
                o = m.outliers[0]
                names = "/".join(labels.get(s, s) for s in agree)
                lines.append(f"- {_ref(g)}: {labels.get(o, o)} {m.field} {m.values[o]} vs "
                             f"{names} {m.values[agree[0]]}")
            else:
                vals = "; ".join(f"{labels.get(s, s)} {v}" for s, v in m.values.items())
                lines.append(f"- {_ref(g)}: {m.field} differs ({vals})")
    return lines


def _pending_proposal(g) -> bool:
    """Group whose rows are proposed as part of another receipt (awaiting confirmation)."""
    return any(f.type == "FUZZY_MATCH_PROPOSAL" and not f.resolved
               for rs in g.members.values() for r in rs for f in r.flags)


def _settled_groups(run: RunResult) -> list:
    """Groups missing from a source with no match proposal or other doubt outstanding."""
    doubtful = {e.group_id for e in run.ledger if e.outcome == Outcome.NEEDS_REVIEW}
    return [g for g in run.match.groups
            if len(g.sources) < len(run.inputs) and not g.proposals
            and not _pending_proposal(g) and g.group_id not in doubtful]


def summary_text(run: RunResult, settings: Settings, report_name: str) -> str:
    """<= ~12 lines: status, exceptions first, counts, proof line."""
    labels = settings.sources.labels
    n_receipts = sum(1 for g in run.match.groups if g.sources and not _pending_proposal(g))
    head = (f"Receiving reconciliation{', ' + run.period if run.period else ''}: "
            f"{len(run.inputs)} sources, {n_receipts} receipts. {run.status}")
    exc = _exception_lines(run, settings)
    k = settings.report.summary_max_items
    lines = [head, f"NEEDS ACTION ({len(exc)}):", *exc[:k]]
    if len(exc) > k:
        lines.append(f"- ...and {len(exc) - k} more in the report")
    if not exc:
        lines[-1] = "NEEDS ACTION: none"
    groups_missing = Counter(s for g in _settled_groups(run) for s in
                             [i.source for i in run.inputs if i.source not in g.sources])
    if groups_missing:
        lines.append("MISSING: " + ", ".join(f"{n} missing from {labels.get(s, s)}"
                                             for s, n in groups_missing.items()))
    other = Counter(e.outcome for e in run.ledger)
    if other.get(Outcome.DUPLICATE_EXTRA) or other.get(Outcome.NON_RECEIPT):
        lines.append(f"DUPLICATES: {other.get(Outcome.DUPLICATE_EXTRA, 0)}, "
                     f"RETURNS/ADJUSTMENTS: {other.get(Outcome.NON_RECEIPT, 0)}")
    lines.append(f"AWAITING REVIEW: {run.awaiting}")
    n = len(run.ledger)
    lines.append(f"PROOF: all {n} rows accounted for, totals tie ✓" if run.proof.ok
                 else f"PROOF FAILED: {len(run.proof.failures())} check(s) - do not rely on this")
    if report_name:
        lines.append(f"Report: {report_name}")
    return "\n".join(lines)


def verify_numbers(text: str, run: RunResult, settings: Settings) -> bool:
    """Every number in a (possibly rephrased) summary must appear in the template's facts."""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", summary_text(run, settings, "")))
    return set(re.findall(r"\d+(?:\.\d+)?", text)) <= allowed


def qty_total(run: RunResult, source: str) -> dict[str, Decimal]:
    """Base-unit totals for one source (for UI badges)."""
    return run.proof.qty_totals.get(source, {})
