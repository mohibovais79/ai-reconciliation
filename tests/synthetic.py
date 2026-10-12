"""Synthetic second dataset with different columns, no key in one source, day-first dates,
different units. Proves the pipeline is general (no code changes, only mapping/config)."""

from __future__ import annotations

import io
import json
from datetime import datetime

from openpyxl import Workbook

# Ground truth receipts: (no, day, vendor, material, batch, qty, unit)
RECEIPTS = [
    (101, 3, "Northwind Foods Ltd", "Rice Flour", "B-7731", 40, "bag"),
    (102, 5, "Contoso Chemicals Inc", "Citric Acid", "B-7732", 25, "kg"),
    (103, 7, "Fabrikam Plastics", "PET Preform 28mm", "B-7733", 1200, "pcs"),
    (104, 13, "Northwind Foods Ltd", "Corn Starch", "B-7734", 30, "bag"),
    (105, 15, "Contoso Chemicals Inc", "Sodium Benzoate", "B-7735", 10, "kg"),
    (106, 18, "Fabrikam Plastics", "HDPE Caps", "B-7736", 5000, "pcs"),
    (107, 21, "Litware Packaging", "Shrink Film", "B-7737", 120, "kg"),
    (108, 24, "Litware Packaging", "Carton 5-ply", "B-7738", 300, "pcs"),
]


def erp_xlsx() -> bytes:
    """ERP export: title rows, real dates, 'RCPT/' keys, a totals row; 104 missing; 105 qty off."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append(["Monthly receipts export"])
    ws.append([])
    ws.append(["Receipt No", "Received On", "Vendor", "Material", "Batch", "Quantity", "UOM",
               "Created By", "Created On"])
    for no, d, v, m, b, q, u in RECEIPTS:
        if no == 104:
            continue
        q2 = 12 if no == 105 else q
        ws.append([f"RCPT/{no:05d}", datetime(2026, 3, d), v.upper(), m, b, q2, u.upper(),
                   "anita", datetime(2026, 3, d, 17, 0)])
    ws.append(["RCPT/00109", datetime(2026, 3, 2), "Litware Packaging", "Tape", "B-7740", 50,
               "PCS", "admin", datetime(2026, 3, 29, 23, 0)])
    ws.append(["Total", None, None, None, None, 9999, None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def store_csv() -> bytes:
    """Store sheet: NO key column, dd-mm-yyyy text dates, citric acid recorded in lb."""
    lines = ["Date;Party Name;Goods;Batch No;Qty;Unit"]
    for no, d, v, m, b, q, u in RECEIPTS:
        if no == 102:
            lines.append(f"{d:02d}-03-2026;{v};{m};{b};55.1156;lbs")
            continue
        lines.append(f"{d:02d}-03-2026;{v};{m};{b};{q};{u}")
    lines.append("")
    return "\n".join(lines).encode()


def register_extraction() -> dict:
    """Register page JSON as a model would return it; 106 qty illegible, 103 qty struck."""
    rows = []
    for no, d, v, m, b, q, u in RECEIPTS:
        cells = [{"text": str(no)}, {"text": f"{d}/3"}, {"text": v}, {"text": m}, {"text": b},
                 {"text": str(q)}, {"text": u}]
        if no == 106:
            cells[5] = {"text": "5", "legibility": "illegible", "note": "ink blot"}
        if no == 103:
            cells[5] = {"text": "1200", "struck_text": "1100"}
        rows.append(cells)
    return {"page_label": "1", "period_label": "March 2026",
            "columns": ["Receipt #", "Dt", "Supplier", "Item", "Batch", "Qty", "Unit"],
            "rows": rows, "rows_visible": len(rows)}


def register_json_text() -> str:
    """Register extraction serialised as model output text."""
    return json.dumps(register_extraction())
