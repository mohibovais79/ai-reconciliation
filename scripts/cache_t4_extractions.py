"""Run live extraction on the t4 register pages and save results as shipped fixtures.

Usage: python scripts/cache_t4_extractions.py
Reads GEMINI_API_KEY from .env; writes sample_data/cached_extractions/<image-sha>.json
so the offline demo and tests can replay the real pages without a key.
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from recon.config import load_settings
from recon.extract import FIXTURE_DIR, run_extraction
from recon.imaging import load_all
from recon.logging_utils import register_secret
from recon.providers import make_provider
from recon.store import Store

s = load_settings()
key = os.environ.get("GEMINI_API_KEY", "")
register_secret(key)
provider = make_provider("gemini", s)
model = s.providers.get("gemini").model

files = sorted(Path("sample_data").glob("t4_register_page*.jpg"))
pages = load_all([(f.name, f.read_bytes()) for f in files], s)

store = Store(s.data_path / "recon.sqlite")
results = run_extraction(pages, provider=provider, api_key=key, model=model, settings=s,
                         store=store, verification="second_pass", on_progress=print)

for page, res in zip(pages, results, strict=True):
    if res.status != "ok" or not res.extraction:
        print(f"{page.name}: FAILED - {res.error}")
        continue
    out = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pass_a": res.extraction.model_dump(),
        "pass_b": res.verify.model_dump() if res.verify else None,
    }
    path = FIXTURE_DIR / f"{page.sha256}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"{page.name}: wrote {path.name} ({len(res.extraction.rows)} rows, "
          f"verify={'yes' if res.verify else 'no'})")
