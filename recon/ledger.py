"""Row ledger: exactly one outcome per source row, by fixed precedence."""

from __future__ import annotations

from recon.match import MatchResult
from recon.models import LedgerEntry, Outcome, SourceRow


def build_ledger(rows: list[SourceRow], mr: MatchResult, active_sources: list[str]
                 ) -> list[LedgerEntry]:
    """NON_RECEIPT > DUPLICATE_EXTRA > NEEDS_REVIEW > MISSING > MISMATCHED > MATCHED."""
    groups = {g.group_id: g for g in mr.groups}
    nr_ids = {r.row_id for r in mr.non_receipts}
    out = []
    for r in rows:
        g = groups.get(mr.group_of.get(r.row_id, ""))
        e = LedgerEntry(row_id=r.row_id, source=r.source, outcome=Outcome.MATCHED,
                        group_id=g.group_id if g else None, key=r.key, qty_base=r.qty_base,
                        base_unit=r.base_unit)
        if g is None:
            for gg in groups.values():
                if any(d.row_id == r.row_id for d in gg.duplicates):
                    g = gg
                    e.group_id = g.group_id
        if g:
            e.missing_from = [s for s in active_sources if s not in g.sources]
            e.mismatched_fields = [m.field for m in g.mismatches]
            e.hints = list(g.hints)
        blocking = r.blocking_flags()
        if r.row_id in nr_ids:
            e.outcome = Outcome.NON_RECEIPT
            e.detail = "; ".join(f.message for f in r.flags if f.type == "NON_RECEIPT_LINK")
        elif r.row_id in mr.duplicates:
            e.outcome = Outcome.DUPLICATE_EXTRA
            e.detail = "identical repeat of another row in the same source"
        elif blocking:
            e.outcome = Outcome.NEEDS_REVIEW
            e.detail = "; ".join(f"{f.type.value}: {f.message}" for f in blocking)
        elif e.missing_from:
            e.outcome = Outcome.MISSING
            e.detail = f"not found in {', '.join(e.missing_from)}"
            if g and g.proposals:
                e.detail += f" (possible match awaiting review: {', '.join(g.proposals)})"
        elif e.mismatched_fields:
            e.outcome = Outcome.MISMATCHED
            e.detail = "; ".join(f"{m.field}: {m.hint}" for m in g.mismatches)
        out.append(e)
    return out
