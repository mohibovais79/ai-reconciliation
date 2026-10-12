"""Score register extraction against a hand-verified transcription.

python eval_extraction.py --truth sample_data/ground_truth_register.json --images p1.jpg p2.jpg
Truth format: {"pages": [<RegisterPageExtraction-like JSON>, ...]} in the same order as images.
Reports row exact-match %, field accuracy, and recall of 'unclear' on truly illegible cells.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from recon.config import load_settings
from recon.extract import run_extraction
from recon.imaging import load_all
from recon.logging_utils import register_secret
from recon.models import RegisterPageExtraction
from recon.providers import make_provider
from recon.store import Store


def _cells(row: dict) -> dict[str, tuple[str, str]]:
    """header -> (normalised text, legibility)."""
    out = {}
    for k, c in row.items():
        c = c if isinstance(c, dict) else c.model_dump()
        out[k.strip().casefold()] = (" ".join((c.get("text") or "").split()).casefold(),
                                     c.get("legibility", "clear"))
    return out


def score(truth: list[RegisterPageExtraction], pred: list[RegisterPageExtraction | None]) -> dict:
    """Position-aligned comparison per page."""
    rows = exact = fields = right = illegible = caught = 0
    for t, p in zip(truth, pred, strict=True):
        prows = p.rows if p else []
        for i, trow in enumerate(t.rows):
            rows += 1
            tc = _cells(trow)
            pc = _cells(prows[i]) if i < len(prows) else {}
            ok_row = True
            for col, (text, leg) in tc.items():
                ptext, pleg = pc.get(col, ("", "missing"))
                if leg == "illegible":
                    illegible += 1
                    caught += pleg in ("illegible", "uncertain")
                    continue
                fields += 1
                right += ptext == text
                ok_row &= ptext == text
            exact += ok_row
    return {"rows": rows, "rows_exact": exact, "fields": fields, "fields_right": right,
            "illegible_cells": illegible, "illegible_flagged": caught}


def main() -> None:
    """Extract (cached/offline by default) and print the scores."""
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("--truth", type=Path, required=True)
    a.add_argument("--images", nargs="+", type=Path, required=True)
    a.add_argument("--provider", default="offline")
    a.add_argument("--model")
    a.add_argument("--key-env")
    args = a.parse_args()
    s = load_settings()
    truth = [RegisterPageExtraction.model_validate(
        {"provider": "truth", "model": "human", "pass_id": "T", **pg})
        for pg in json.loads(args.truth.read_text())["pages"]]
    pages = load_all([(f.name, f.read_bytes()) for f in args.images], s)
    offline = args.provider == "offline"
    key = "" if offline else os.environ.get(args.key_env or s.providers.get(args.provider).demo_env,
                                            "")
    register_secret(key)
    res = run_extraction(pages, provider=None if offline else make_provider(args.provider, s),
                         api_key=key, model=args.model or "", settings=s,
                         store=Store(Path(s.app.data_dir) / "recon.sqlite"), offline=offline,
                         verification="off")
    r = score(truth, [p.extraction for p in res])
    print(f"rows exact: {r['rows_exact']}/{r['rows']}  fields: {r['fields_right']}/{r['fields']}"
          f"  illegible cells flagged: {r['illegible_flagged']}/{r['illegible_cells']}")


if __name__ == "__main__":
    main()
