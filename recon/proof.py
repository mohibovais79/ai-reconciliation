"""Proof of completeness: every source row lands in exactly one outcome; totals tie."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from recon.models import DroppedRow, LedgerEntry, Outcome, SourceRow


class ProofFailed(AssertionError):
    """Raised when a completeness invariant does not hold. The run must not be shown as done."""


@dataclass
class ProofCheck:
    """One invariant evaluated for one source."""

    name: str
    source: str
    expected: str
    actual: str
    ok: bool


@dataclass
class ProofResult:
    """All checks plus per-source outcome counts."""

    checks: list[ProofCheck] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    qty_totals: dict[str, dict[str, Decimal]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when every check passed."""
        return all(c.ok for c in self.checks)

    def failures(self) -> list[ProofCheck]:
        """Checks that failed."""
        return [c for c in self.checks if not c.ok]


def _qty_by_unit(items) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for it in items:
        if it.qty_base is not None:
            out[it.base_unit or "(no unit)"] += it.qty_base
    return dict(out)


def check(rows_read: dict[str, int], rows: list[SourceRow], dropped: list[DroppedRow],
          ledger: list[LedgerEntry], rows_before_review: dict[str, int] | None = None
          ) -> ProofResult:
    """Evaluate all invariants (§9) and return the result without raising."""
    res = ProofResult()
    add = res.checks.append
    ids = Counter(e.row_id for e in ledger)
    dupes = [i for i, n in ids.items() if n > 1]
    row_ids = {r.row_id for r in rows}
    add(ProofCheck("each row appears exactly once in the ledger", "ALL",
                   "0 repeated ids", f"{len(dupes)} repeated ids", not dupes))
    missing = row_ids - set(ids)
    extra = set(ids) - row_ids
    add(ProofCheck("ledger covers every ingested row and nothing else", "ALL",
                   "0 missing / 0 extra", f"{len(missing)} missing / {len(extra)} extra",
                   not missing and not extra))
    for src in rows_read:
        s_rows = [r for r in rows if r.source == src]
        s_drop = [d for d in dropped if d.source == src]
        s_led = [e for e in ledger if e.source == src]
        n_read, n_in, n_drop = rows_read[src], len(s_rows), len(s_drop)
        add(ProofCheck("rows read = rows ingested + rows dropped", src,
                       str(n_read), f"{n_in} + {n_drop} = {n_in + n_drop}",
                       n_read == n_in + n_drop))
        counts = Counter(e.outcome.value for e in s_led)
        res.counts[src] = {o.value: counts.get(o.value, 0) for o in Outcome}
        add(ProofCheck("rows ingested = sum of outcome counts", src, str(n_in),
                       str(sum(counts.values())), n_in == sum(counts.values())))
        q_rows, q_led = _qty_by_unit(s_rows), _qty_by_unit(s_led)
        res.qty_totals[src] = q_rows
        for unit in sorted(set(q_rows) | set(q_led)):
            a, b = q_rows.get(unit, Decimal(0)), q_led.get(unit, Decimal(0))
            add(ProofCheck(f"quantity total ties ({unit})", src, str(a), str(b), a == b))
        if rows_before_review is not None:
            before = rows_before_review.get(src, 0)
            add(ProofCheck("review decisions never delete rows", src, str(before), str(n_in),
                           before == n_in))
    return res


def assert_complete(result: ProofResult) -> None:
    """Fail loudly if any invariant is broken."""
    if not result.ok:
        lines = [f"{c.source}: {c.name} (expected {c.expected}, got {c.actual})"
                 for c in result.failures()]
        raise ProofFailed("Completeness proof failed:\n" + "\n".join(lines))
