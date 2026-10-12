"""Write the synthetic demo dataset + its cached register extraction (offline demo, no key)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recon.extract import FIXTURE_DIR, parse_extraction  # noqa: E402
from tests import synthetic  # noqa: E402

OUT = ROOT / "sample_data" / "synthetic"


def render_register(data: dict) -> bytes:
    """Draw the register page as a simple table image (stand-in for a photo)."""
    cols, rows = data["columns"], data["rows"]
    w, h, cw = 1400, 140 + 50 * (len(rows) + 1), 1400 // len(cols)
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 20), f"Receiving register  {data['period_label']}  page {data['page_label']}",
           fill="black")
    for j, c in enumerate(cols):
        d.text((20 + j * cw, 90), c, fill="black")
    for i, row in enumerate(rows):
        y = 140 + 50 * i
        for j, cell in enumerate(row):
            text = cell.get("text") or ""
            if cell.get("struck_text"):
                d.text((20 + j * cw, y), cell["struck_text"], fill="gray")
                d.line((20 + j * cw, y + 6, 70 + j * cw, y + 6), fill="black", width=2)
                text = "   " + text
            d.text((20 + j * cw + (40 if cell.get("struck_text") else 0), y), text, fill="black")
            if cell.get("legibility") == "illegible":
                d.ellipse((25 + j * cw, y - 5, 75 + j * cw, y + 18), fill="navy")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    """Write files and the sha-keyed extraction fixture."""
    OUT.mkdir(parents=True, exist_ok=True)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    data = synthetic.register_extraction()
    png = render_register(data)
    (OUT / "register_page1.png").write_bytes(png)
    (OUT / "erp_export.xlsx").write_bytes(synthetic.erp_xlsx())
    (OUT / "store_sheet.csv").write_bytes(synthetic.store_csv())
    text = json.dumps(data)
    def dump(pid: str) -> dict:
        return parse_extraction(text, "fixture", "synthetic", pid).model_dump(mode="json")

    fixture = {"created_at": "synthetic fixture", "pass_a": dump("A"), "pass_b": dump("B")}
    sha = hashlib.sha256(png).hexdigest()
    (FIXTURE_DIR / f"{sha}.json").write_text(json.dumps(fixture, indent=1))
    print(f"wrote {OUT} and fixture {sha[:12]}")


if __name__ == "__main__":
    main()
