"""Milestone 3: imaging, parsing, repair retry, two-pass compare, cache, failure handling."""

import io
import json

import pytest
from PIL import Image

from recon.config import load_settings
from recon.extract import compare_passes, duplicate_pages, parse_extraction, run_extraction
from recon.imaging import ImageRejected, load_pages
from recon.providers.base import AuthError, ServerError
from recon.providers.mock import MockProvider
from recon.store import Store

S = load_settings(overrides={"providers": {"backoff_base_s": 0, "backoff_max_s": 0}})


def jpeg_with_exif(w=3000, h=1000, color=(200, 200, 200)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    exif = Image.Exif()
    exif[0x0112] = 6            # orientation: rotate 90
    exif[0x8825] = {1: "N"}     # GPS IFD
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def page_json(rows, cols=("GRN", "Qty"), visible=None, **cell_over):
    return json.dumps({"columns": list(cols), "rows_visible": visible or len(rows),
                       "rows": [[{"text": v} for v in r] for r in rows]})


def test_imaging_orients_strips_and_resizes():
    [p] = load_pages("a.jpg", jpeg_with_exif(), S)
    img = Image.open(io.BytesIO(p.jpeg))
    assert max(img.size) <= S.app.image_max_side
    assert img.size[1] > img.size[0]          # rotated by EXIF orientation
    assert not img.getexif()                   # metadata gone


def test_imaging_rejects():
    with pytest.raises(ImageRejected):
        load_pages("a.gif", b"GIF89a", S)
    with pytest.raises(ImageRejected):
        load_pages("a.jpg", b"not an image", S)
    big = load_settings(overrides={"app": {"max_file_mb": 0}})
    with pytest.raises(ImageRejected):
        load_pages("a.jpg", jpeg_with_exif(10, 10), big)


def test_parse_handles_fences_and_dict_rows():
    text = "Here you go:\n```json\n" + json.dumps({
        "columns": ["A", "B"], "rows": [{"A": "1", "B": {"text": "x", "legibility": "illegible"}}],
    }) + "\n```"
    ext = parse_extraction(text, "mock", "m", "A")
    assert ext.rows[0]["A"].text == "1" and ext.rows[0]["B"].legibility == "illegible"
    with pytest.raises(ValueError):
        parse_extraction("no json here", "mock", "m", "A")


def test_parse_handles_escaped_quotes_in_cells():
    """A value like 63\\" must not desync the brace scanner."""
    text = json.dumps({"columns": ["Item", "Qty"],
                       "rows": [[{"text": 'Grey fabric 63"'}, {"text": "1200"}]],
                       "rows_visible": 1})
    ext = parse_extraction(text, "mock", "m", "A")
    assert ext.rows[0]["Item"].text == 'Grey fabric 63"'


def _pages(n=1):
    return [load_pages(f"p{i}.jpg", jpeg_with_exif(50 + i, 40), S)[0] for i in range(n)]


def test_repair_retry_then_cache_hit(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    pages = _pages(1)
    prov = MockProvider(script=["garbage", page_json([["1", "5"]]), page_json([["1", "5"]])])
    res = run_extraction(pages, provider=prov, api_key="k", model="m", settings=S, store=store)
    assert res[0].status == "ok" and len(prov.calls) == 3   # A bad, A repaired, B
    assert "previous answer could not be used" not in prov.calls[0]["prompt"]
    prov2 = MockProvider(script=[])
    res2 = run_extraction(pages, provider=prov2, api_key="k", model="m", settings=S, store=store)
    assert res2[0].cached and prov2.calls == []


def test_failed_page_is_recorded_not_dropped(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    prov = MockProvider(script=["bad", "still bad", page_json([["2", "1"]]),
                                page_json([["2", "1"]])])
    res = run_extraction(_pages(2), provider=prov, api_key="k", model="m", settings=S,
                         store=store)
    assert [r.status for r in res] == ["failed", "ok"]
    assert "invalid output" in res[0].error


def test_auth_error_not_retried_and_transient_retried(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    prov = MockProvider(script=[AuthError("mock", "bad key", 401)])
    res = run_extraction(_pages(1), provider=prov, api_key="k", model="m", settings=S,
                         store=store, verification="off")
    assert res[0].status == "failed" and "rejected" in res[0].error and len(prov.calls) == 1
    prov = MockProvider(script=[ServerError("mock", "x", 503), page_json([["1", "2"]])])
    msgs = []
    res = run_extraction(_pages(1), provider=prov, api_key="k", model="m2", settings=S,
                         store=store, verification="off", on_progress=msgs.append)
    assert res[0].status == "ok" and any("retrying (2/4)" in m for m in msgs)


def test_compare_passes_flags_differences():
    a = parse_extraction(page_json([["1", "5"], ["2", "60"], ["3", "7"]]), "m", "m", "A")
    b = parse_extraction(page_json([["1", "5"], ["2", "66"], ["3", "7"]]), "m", "m", "B")
    rows, page = compare_passes(a, b)
    assert list(rows) == [1] and rows[1][0].field == "Qty" and not page
    c = parse_extraction(page_json([["1", "5"], ["3", "7"]]), "m", "m", "B")
    rows, page = compare_passes(a, c)
    assert 1 in rows and page


def test_duplicate_page_warning():
    p = _pages(1)[0]
    assert duplicate_pages([p, p])


def test_eval_scoring_counts_exact_rows_and_illegible_recall():
    from eval_extraction import score
    from tests import synthetic
    truth = parse_extraction(synthetic.register_json_text(), "t", "h", "T")
    pred = truth.model_copy(deep=True)
    first = next(iter(pred.rows[0]))
    pred.rows[0][first].text = "999"
    r = score([truth], [pred])
    assert r["rows"] == 8 and r["rows_exact"] == 7
    assert r["illegible_cells"] == 1 and r["illegible_flagged"] == 1
