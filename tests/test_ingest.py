"""Milestone 4: ingest, header detection, dropped-row accounting, auto mapping, parsing."""

import datetime as dt
from decimal import Decimal

import pytest

from recon.config import load_settings
from recon.extract import parse_extraction
from recon.ingest import auto_map, missing_required, read_table, register_table, to_source_rows
from recon.models import FlagType, PageResult
from recon.normalize import (
    canonical_unit,
    detect_dayfirst,
    normalize_key,
    normalize_supplier,
    parse_date,
    parse_qty,
    to_base,
)
from tests import synthetic

S = load_settings()


@pytest.mark.parametrize("raw,val", [("1,00,000", "100000"), ("(5)", "-5"), ("-5000", "-5000"),
                                     ("1 234.50", "1234.50"), (46.0, "46"), ("48 pcs", "48")])
def test_parse_qty(raw, val):
    assert parse_qty(raw)[0] == Decimal(val)


def test_parse_qty_rejects_text():
    assert parse_qty("approx ten")[0] is None and parse_qty("1.2.3")[0] is None


def test_dates_and_order():
    assert detect_dayfirst(["14-03-2026", "02-03-2026"]) is True
    assert detect_dayfirst(["9/14/2026"]) is False
    assert detect_dayfirst(["03/04/2026"]) is None
    assert parse_date("14/3", True, 2026) == dt.date(2026, 3, 14)
    assert parse_date(dt.datetime(2026, 3, 1, 5), False) == dt.date(2026, 3, 1)
    assert parse_date("2026-03-04", True) == dt.date(2026, 3, 4)


def test_units_keys_suppliers():
    assert canonical_unit("Yds.", S) == "yd" and canonical_unit("LBS", S) == "lb"
    assert canonical_unit("zorks", S) is None
    assert to_base(Decimal(100), "yd", S) == (Decimal("91.4400"), "m")
    assert normalize_key("GRN-0044", "GRN") == normalize_key("44") == "44"
    assert normalize_supplier("ACME Pvt. Ltd.", S) == normalize_supplier("Acme", S)


def test_erp_xlsx_header_drops_and_mapping():
    t = read_table("ERP", "erp.xlsx", synthetic.erp_xlsx(), S)
    assert t.header_row == 3
    reasons = sorted(d.reason for d in t.dropped)
    assert reasons == ["above header (title/notes)"] * 2 + ["totals row"]
    assert t.rows_read == len(t.records) + len(t.dropped)
    m = auto_map(t, S)
    assert (m["key"], m["date"], m["supplier"], m["item"], m["lot"], m["qty"], m["unit"]) == (
        "Receipt No", "Received On", "Vendor", "Material", "Batch", "Quantity", "UOM")
    assert m["entered_by"] == "Created By" and m["entered_at"] == "Created On"


def test_store_csv_no_key_dayfirst_units():
    t = read_table("STORE", "store.csv", synthetic.store_csv(), S)
    m = auto_map(t, S)
    assert m["key"] is None and m["qty"] == "Qty" and m["supplier"] == "Party Name"
    assert m["item"] == "Goods" and m["lot"] == "Batch No" and m["date"] == "Date"
    assert missing_required(m, S) == []
    rows, ctx = to_source_rows(t, m, S)
    assert ctx.dayfirst is True
    citric = next(r for r in rows if r.item_norm == "citric acid")
    assert citric.unit == "lb" and citric.base_unit == "kg"
    assert abs(citric.qty_base - Decimal(25)) < Decimal("0.01")
    assert rows[0].date == dt.date(2026, 3, 3)


def test_register_table_flags_and_year():
    ext = parse_extraction(synthetic.register_json_text(), "mock", "m", "A")
    res = [PageResult(page_index=0, file_name="p1.jpg", sha256="x", status="ok", extraction=ext)]
    t = register_table(res)
    m = auto_map(t, S)
    assert m["key"] == "Receipt #" and m["date"] == "Dt" and m["qty"] == "Qty"
    rows, ctx = to_source_rows(t, m, S)
    assert ctx.default_year == 2026 and rows[0].date == dt.date(2026, 3, 3)
    blot = next(r for r in rows if r.key == "106")
    assert blot.qty is None
    assert any(f.type == FlagType.UNCLEAR_FIELD and f.field == "qty" for f in blot.flags)
    struck = next(r for r in rows if r.key == "103")
    sv = next(f for f in struck.flags if f.type == FlagType.STRUCK_VALUE)
    assert sv.suggestion == "1200" and "1100" in sv.candidates


def test_failed_page_contributes_no_rows():
    ext = parse_extraction(synthetic.register_json_text(), "mock", "m", "A")
    res = [PageResult(page_index=0, file_name="p1", sha256="a", status="failed", error="x"),
           PageResult(page_index=1, file_name="p2", sha256="b", status="ok", extraction=ext)]
    t = register_table(res)
    assert t.rows_read == 8 and t.records[0][0] == "p2r1"
