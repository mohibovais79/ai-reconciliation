"""Milestone 5: partition invariants under random data (hypothesis) + proof failure modes."""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from recon.config import load_settings
from recon.ledger import build_ledger
from recon.match import build_groups, compare_group, flag_suspect_entries
from recon.models import DroppedRow, Flag, FlagType, LedgerEntry, Outcome, SourceRow
from recon.proof import ProofFailed, assert_complete, check

S = load_settings()
SOURCES = ["REG", "ERP", "STORE"]


@st.composite
def source_rows(draw):
    rows = []
    for src in SOURCES:
        n = draw(st.integers(0, 12))
        for i in range(n):
            q = draw(st.one_of(st.none(), st.integers(-50, 500)))
            unit = draw(st.sampled_from([None, "pcs", "m", "kg"]))
            flags = []
            if draw(st.booleans()) and draw(st.booleans()):
                flags.append(Flag(type=FlagType.UNCLEAR_FIELD, field="qty"))
            rows.append(SourceRow(
                source=src, row_id=f"{src}:r{i + 2}",
                key=draw(st.one_of(st.none(), st.sampled_from(["1", "2", "3", "4", "12", "21"]))),
                supplier_norm=draw(st.sampled_from(["acme", "zeta", None])),
                item_norm=draw(st.sampled_from(["bolt", "nut", None])),
                lot=draw(st.sampled_from(["L1", "L2", None])),
                qty=None if q is None else Decimal(q), qty_base=None if q is None else Decimal(q),
                unit=unit, base_unit=unit, flags=flags))
    return rows


@settings(max_examples=150, deadline=None)
@given(source_rows(), st.dictionaries(st.sampled_from(SOURCES), st.integers(0, 3)))
def test_partition_invariants_hold(rows, n_dropped):
    dropped = [DroppedRow(source=s, row_ref=f"d{i}", reason="blank row")
               for s, n in n_dropped.items() for i in range(n)]
    read = {s: sum(r.source == s for r in rows) + n_dropped.get(s, 0) for s in SOURCES}
    mr = build_groups(rows, S)
    for g in mr.groups:
        compare_group(g, S)
    flag_suspect_entries(mr.groups, rows, S)
    ledger = build_ledger(rows, mr, SOURCES)
    res = check(read, rows, dropped, ledger)
    assert res.ok, [c for c in res.failures()]
    assert len(ledger) == len(rows)


def _row(i, src="ERP", q=1):
    return SourceRow(source=src, row_id=f"{src}:r{i}", qty=Decimal(q), qty_base=Decimal(q),
                     base_unit="pcs")


def test_proof_detects_missing_duplicate_and_count_errors():
    rows = [_row(1), _row(2)]
    good = [LedgerEntry(row_id=r.row_id, source="ERP", outcome=Outcome.MATCHED,
                        qty_base=r.qty_base, base_unit="pcs") for r in rows]
    assert check({"ERP": 2}, rows, [], good).ok
    with pytest.raises(ProofFailed):
        assert_complete(check({"ERP": 2}, rows, [], good[:1]))
    with pytest.raises(ProofFailed):
        assert_complete(check({"ERP": 2}, rows, [], good + good[:1]))
    with pytest.raises(ProofFailed):
        assert_complete(check({"ERP": 3}, rows, [], good))
    bad_qty = [good[0], good[1].model_copy(update={"qty_base": Decimal(5)})]
    with pytest.raises(ProofFailed):
        assert_complete(check({"ERP": 2}, rows, [], bad_qty))
    with pytest.raises(ProofFailed):
        assert_complete(check({"ERP": 2}, rows, [], good, rows_before_review={"ERP": 3}))
