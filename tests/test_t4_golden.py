"""Golden regression on the Kestrel t4_* dataset using the shipped cached extractions.

The expected findings (AGENT_PLAN §15) must be produced by the general rules —
column mapping, normalisation, tiered matching — with zero dataset-specific code.
Register pages replay from sample_data/cached_extractions, so no API key is needed.
"""

from decimal import Decimal

import pytest

from recon.config import ROOT, load_settings
from recon.extract import run_extraction
from recon.imaging import load_all
from recon.ingest import auto_map, read_table, register_table
from recon.models import Outcome
from recon.pipeline import reconcile
from recon.report import build_workbook
from recon.store import Store

S = load_settings()
DATA = ROOT / "sample_data"


@pytest.fixture(scope="module")
def run():
    store = Store(":memory:")
    pages = load_all([(f"t4_register_page{i}.jpg",
                       (DATA / f"t4_register_page{i}.jpg").read_bytes())
                      for i in (1, 2)], S)
    res = run_extraction(pages, provider=None, api_key="", model="", settings=S,
                         store=store, offline=True)
    assert all(p.status == "ok" for p in res), "cached t4 extractions missing"
    tables = {"REG": register_table(res),
              "ERP": read_table("ERP", "t4_erp_grn.csv",
                                (DATA / "t4_erp_grn.csv").read_bytes(), S),
              "STORE": read_table("STORE", "t4_store_sheet.xlsx",
                                  (DATA / "t4_store_sheet.xlsx").read_bytes(), S)}
    maps = {k: auto_map(t, S) for k, t in tables.items()}
    return reconcile(tables, maps, S, store, res, 2026)


def _group(run, key):
    return next(g for g in run.match.groups if g.key == key)


def _field(run, key, field):
    g = _group(run, key)
    return next((m for m in g.mismatches if m.field == field), None)


def _outcome(run, row_id):
    return next(e.outcome for e in run.ledger if e.row_id == row_id)


def test_counts_and_proof(run):
    assert run.proof.ok and run.complete
    counts = {i.source: i.rows_ingested for i in run.inputs}
    assert counts == {"REG": 28, "ERP": 28, "STORE": 30}


def test_receipt_missing_from_erp(run):
    missing = [e for e in run.ledger if e.outcome == Outcome.MISSING]
    keys = {(e.key, tuple(e.missing_from)) for e in missing}
    assert ("4421", ("ERP",)) in keys


def test_lot_transposition_4412(run):
    m = _field(run, "4412", "lot")
    assert m and m.outliers == ["ERP"]
    assert any("TRANSPOSITION_HINT" in h for h in _group(run, "4412").hints)


def test_erp_date_off_by_one_4415(run):
    m = _field(run, "4415", "date")
    assert m and m.outliers == ["ERP"]
    assert any("DATE_OFF_BY_ONE" in h for h in _group(run, "4415").hints)


def test_erp_qty_outlier_4417(run):
    m = _field(run, "4417", "qty")
    assert m and m.outliers == ["ERP"]


def test_struck_qty_and_stale_store_4419(run):
    struck = [q for q in run.queue if q.type == "STRUCK_VALUE"]
    assert struck and struck[0].row_id.startswith("REG:")
    m = _field(run, "4419", "qty")
    assert m and m.outliers == ["STORE"]      # store still has the crossed-out value


def test_fabric_units_4425(run):
    g = _group(run, "4425")
    reg, erp = g.primary("REG"), g.primary("ERP")
    assert reg.qty_base == erp.qty_base == Decimal("1097.28")   # yd -> m conversion
    assert reg.base_unit == erp.base_unit == "m"
    m = _field(run, "4425", "qty")
    assert m and m.outliers == ["STORE"]
    assert any("UNIT_MISLABEL_SUSPECTED" in h for h in g.hints)


def test_blotted_qty_is_reviewed_not_guessed_4428(run):
    unclear = [q for q in run.queue if q.type == "UNCLEAR_FIELD"]
    assert unclear and unclear[0].field == "qty" and unclear[0].blocking
    assert _outcome(run, unclear[0].row_id) == Outcome.NEEDS_REVIEW


def test_store_duplicate_4409(run):
    dups = [e for e in run.ledger if e.outcome == Outcome.DUPLICATE_EXTRA]
    assert len(dups) == 1 and dups[0].source == "STORE" and dups[0].key == "4409"


def test_return_row_is_non_receipt_with_link(run):
    nr = [e for e in run.ledger if e.outcome == Outcome.NON_RECEIPT]
    assert len(nr) == 1 and nr[0].source == "STORE"
    link = [q for q in run.queue if q.type == "NON_RECEIPT_LINK"]
    assert link and link[0].suggestion == "K:4402"


def test_erp_only_backdated_entry_is_suspect_4430(run):
    suspect = [q for q in run.queue if q.type == "SUSPECT_ENTRY"]
    assert suspect and suspect[0].row_id.startswith("ERP:")
    assert _outcome(run, suspect[0].row_id) == Outcome.NEEDS_REVIEW


def test_text_date_and_sequence_gap(run):
    store_row = next(r for r in run.rows
                     if r.source == "STORE" and r.key == "4416")
    assert store_row.date and store_row.date.isoformat() == "2026-09-14"
    assert store_row.date_was_text
    assert run.match.sequence_gaps == ["4429"]


def test_workbook_builds(run):
    assert build_workbook(run, S, []).startswith(b"PK")
