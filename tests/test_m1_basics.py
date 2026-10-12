"""Milestone 1: config, models, redaction, store."""

import logging
from decimal import Decimal

from recon.config import load_settings
from recon.logging_utils import RedactionFilter, redact, register_secret
from recon.models import Cell, RegisterPageExtraction
from recon.store import Store


def test_config_loads_and_overrides():
    s = load_settings()
    assert s.tolerances.qty_abs == Decimal("0.01")
    assert s.units.conversions[0].factor > 0
    s2 = load_settings(overrides={"tolerances": {"qty_rel": "0.02"}})
    assert s2.tolerances.qty_rel == Decimal("0.02")


def test_models_parse():
    p = RegisterPageExtraction(columns=["A"], rows=[{"A": Cell(text="1")}])
    assert p.rows[0]["A"].legibility == "clear"


def test_redact_patterns_and_registered():
    assert "AIzaSy" not in redact("key AIzaSyA1234567890abcdefghijklmnop")
    assert "abc" not in redact("x-goog-api-key: abcdefghijklmnop")
    register_secret("plain-custom-secret-xyz")
    assert "plain-custom-secret-xyz" not in redact("err plain-custom-secret-xyz here")


def test_logging_filter(caplog):
    register_secret("SENTINEL-LOG-999")
    logger = logging.getLogger("t")
    caplog.handler.addFilter(RedactionFilter())
    logger.warning("using %s", "SENTINEL-LOG-999")
    assert "SENTINEL-LOG-999" not in caplog.text


def test_store_roundtrip(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.put_page("k", "sha", "mock", "m", "v1", "A", {"a": 1})
    assert st.get_page("k")["payload"] == {"a": 1}
    st.put_mapping("fp", "ERP", {"GRN": "key"})
    assert st.get_mapping("fp") == {"GRN": "key"}
    st.put_alias("supplier", "abc tex", "abc textiles")
    assert st.aliases("supplier") == {"abc tex": "abc textiles"}
