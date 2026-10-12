"""M5/M6: synthetic dataset end to end (generality), review decisions persist and invalidate."""

import json

from recon.config import load_settings
from recon.extract import parse_extraction
from recon.ingest import auto_map, read_table, register_table
from recon.models import Outcome, PageResult
from recon.pipeline import reconcile
from recon.review import record_decision, strong_proposals
from recon.store import Store
from tests import synthetic

S = load_settings()


def _tables(reg_json: str | None = None):
    ext = parse_extraction(reg_json or synthetic.register_json_text(), "mock", "m", "A")
    pr = PageResult(page_index=0, file_name="p1.jpg", sha256="x", status="ok", extraction=ext)
    t = {"REG": register_table([pr]),
         "ERP": read_table("ERP", "erp.xlsx", synthetic.erp_xlsx(), S),
         "STORE": read_table("STORE", "store.csv", synthetic.store_csv(), S)}
    return t, {k: auto_map(v, S) for k, v in t.items()}, [pr]


def _run(store, reg_json=None):
    t, m, pages = _tables(reg_json)
    return reconcile(t, m, S, store, pages)


def _outcomes(run):
    return {e.row_id: e.outcome for e in run.ledger}


def _confirm_all(store, run):
    for q in strong_proposals(run.queue, S.matching.tier2_min_score):
        record_decision(store, q, "confirm_match", q.suggestion, "tester")


def test_unkeyed_source_only_proposes_and_proof_ties():
    store = Store(":memory:")
    run = _run(store)
    assert run.proof.ok and not run.complete or run.awaiting
    store_rows = [e for e in run.ledger if e.source == "STORE"]
    assert all(e.outcome == Outcome.NEEDS_REVIEW for e in store_rows)   # never auto-merged
    pairs = [frozenset((q.row_id, q.suggestion)) for q in run.queue
             if q.type == "FUZZY_MATCH_PROPOSAL"]
    assert len(pairs) == len(set(pairs))


def test_confirmed_matches_reveal_findings_by_general_rules():
    store = Store(":memory:")
    _confirm_all(store, _run(store))
    run = _run(store)
    assert run.proof.ok
    out = _outcomes(run)
    assert all(o != Outcome.NEEDS_REVIEW for r, o in out.items() if r.startswith("STORE"))
    erp_keys = {e.key for e in run.ledger if e.source == "ERP"}
    missing = [e for e in run.ledger if e.outcome == Outcome.MISSING]
    assert missing and all("ERP" in e.missing_from for e in missing)
    assert all(e.key not in erp_keys for e in missing)
    mism = [g for g in run.match.groups if g.mismatches]
    assert any(m.field == "qty" and m.outliers == ["ERP"] for g in mism for m in g.mismatches)
    types = {q.type for q in run.queue}
    assert {"UNCLEAR_FIELD", "STRUCK_VALUE", "SUSPECT_ENTRY"} <= types


def test_decision_moves_outcome_survives_rerun_and_invalidates_on_change():
    store = Store(":memory:")
    _confirm_all(store, _run(store))
    run = _run(store)
    blot = next(q for q in run.queue if q.type == "UNCLEAR_FIELD" and q.field == "qty")
    assert _outcomes(run)[blot.row_id] == Outcome.NEEDS_REVIEW
    value = next(iter(blot.hints.values())).split()[0]
    record_decision(store, blot, "enter_value", value, "tester")
    before = run.awaiting
    run2 = _run(store)
    assert _outcomes(run2)[blot.row_id] in (Outcome.MATCHED, Outcome.MISMATCHED)
    assert run2.awaiting == before - 1 and len(run2.ledger) == len(run.ledger)
    data = synthetic.register_extraction()
    idx = int(blot.row_id.rsplit("r", 1)[1]) - 1
    cell = next(c for c in data["rows"][idx] if c.get("legibility") == "illegible")
    cell["text"] += "0"
    run3 = _run(store, json.dumps(data))
    assert run3.stale_decisions >= 1
    assert _outcomes(run3)[blot.row_id] == Outcome.NEEDS_REVIEW


def test_failed_page_makes_run_incomplete():
    store = Store(":memory:")
    t, m, pages = _tables()
    bad = PageResult(page_index=1, file_name="p2.jpg", sha256="y", status="failed",
                     error="model returned invalid JSON twice")
    run = reconcile(t, m, S, store, pages + [bad])
    assert run.status.startswith("INCOMPLETE") and "p2.jpg" in run.status and not run.complete
