"""Streamlit smoke test (offline demo) and the sentinel-key leak test across every output."""

import json
import logging
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from recon.config import load_settings
from recon.extract import run_extraction
from recon.imaging import load_all
from recon.ingest import auto_map, read_table, register_table
from recon.logging_utils import install_redaction
from recon.pipeline import reconcile
from recon.providers import make_provider
from recon.report import build_workbook, ledger_csv, summary_text
from recon.store import Store
from tests import synthetic

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "sample_data" / "synthetic"
SENTINEL = "SENTINEL-KEY-123"


def test_app_offline_happy_path():
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60)
    at.run()
    assert not at.exception
    at.sidebar.selectbox(key="provider").set_value("offline").run()
    next(b for b in at.button if b.label.startswith("Use the bundled demo")).click().run()
    next(b for b in at.button if b.label == "Read register pages").click().run()
    assert not at.exception
    next(b for b in at.button if b.label == "Run reconciliation").click().run()
    assert not at.exception
    run = at.session_state["run"]
    assert run.proof.ok and run.complete
    confirm = [b for b in at.button if b.label.startswith("Confirm all")]
    assert confirm
    confirm[0].click().run()
    assert not at.exception and at.session_state["run"].proof.ok


def _gemini_transport(seen: list):
    text = json.dumps(synthetic.register_extraction())

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert SENTINEL not in str(req.url)
        if len(seen) == 1:      # first call: server error that echoes the key back
            return httpx.Response(500, text=f"internal error for key {SENTINEL}")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return httpx.MockTransport(handler)


def test_sentinel_key_never_leaks(tmp_path, caplog, monkeypatch):
    s = load_settings(overrides={"providers": {"backoff_base_s": 0, "backoff_max_s": 0}})
    db_path = tmp_path / "recon.sqlite"
    store = Store(db_path)
    seen: list = []
    prov = make_provider("gemini", s, transport=_gemini_transport(seen))
    pages = load_all([("p1.png", (DEMO / "register_page1.png").read_bytes())], s)
    install_redaction(caplog.handler)
    caplog.set_level(logging.DEBUG)
    res = run_extraction(pages, provider=prov, api_key=SENTINEL, model="m", settings=s,
                         store=store)
    assert seen and seen[-1].headers["x-goog-api-key"] == SENTINEL
    assert all(r.status == "ok" for r in res)
    bad = make_provider("gemini", s, transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text=f"bad key {SENTINEL}")))
    fail = run_extraction(pages, provider=bad, api_key=SENTINEL, model="other", settings=s,
                          store=store)
    assert fail[0].status == "failed" and SENTINEL not in (fail[0].error or "")
    with pytest.raises(Exception) as ei:
        bad.extract(image_jpeg=b"x", prompt="p", schema=None, api_key=SENTINEL, model="m",
                    timeout=5)
    assert SENTINEL not in str(ei.value) and SENTINEL not in repr(ei.value)
    tables = {"REG": register_table(res),
              "ERP": read_table("ERP", "e.xlsx", synthetic.erp_xlsx(), s),
              "STORE": read_table("STORE", "s.csv", synthetic.store_csv(), s)}
    run = reconcile(tables, {k: auto_map(t, s) for k, t in tables.items()}, s, store, res)
    xlsx = build_workbook(run, s, store.all_decisions())
    store.close()
    blobs = {"sqlite": db_path.read_bytes(), "xlsx": xlsx, "csv": ledger_csv(run),
             "summary": summary_text(run, s, "r.xlsx").encode(), "logs": caplog.text.encode(),
             "pages": json.dumps([r.model_dump(mode="json") for r in res + fail]).encode()}
    for f in tmp_path.rglob("*"):
        if f.is_file():
            blobs[str(f)] = f.read_bytes()
    for name, data in blobs.items():
        assert SENTINEL.encode() not in data, name
