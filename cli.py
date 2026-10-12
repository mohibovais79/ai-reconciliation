"""Headless run: python cli.py --register p1.jpg --erp a.csv --store b.xlsx [--provider offline].

Keys are read from an environment variable (--key-env), never from the command line.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from recon.config import load_settings
from recon.extract import run_extraction
from recon.imaging import load_all
from recon.ingest import auto_map, missing_required, read_table, register_table
from recon.logging_utils import register_secret, setup_logging
from recon.pipeline import reconcile
from recon.providers import make_provider
from recon.report import build_workbook, ledger_csv, mismatched_csv, summary_text
from recon.store import Store


def _mapping(table, settings, store):
    """Saved profile for this header set, else automatic mapping."""
    saved = store.get_mapping(table.fingerprint)
    m = saved or auto_map(table, settings)
    miss = missing_required(m, settings)
    if miss:
        sys.exit(f"{table.name}: no column found for {', '.join(miss)}. Map it in the app first.")
    return m


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline and write report files. Exit 0 complete, 2 awaiting review, 1 failed."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--register", nargs="*", default=[], type=Path)
    p.add_argument("--erp", required=True, type=Path)
    p.add_argument("--store", required=True, type=Path)
    p.add_argument("--provider", default="offline",
                   choices=["offline", "gemini", "openrouter", "groq"])
    p.add_argument("--model")
    p.add_argument("--key-env", help="name of the env var holding the API key")
    p.add_argument("--period-year", type=int)
    p.add_argument("--out", type=Path, default=Path("out"))
    a = p.parse_args(argv)
    setup_logging()
    s = load_settings()
    store = Store(Path(s.app.data_dir) / "recon.sqlite")
    tables = {}
    pages = []
    if a.register:
        imgs = load_all([(f.name, f.read_bytes()) for f in a.register], s)
        offline = a.provider == "offline"
        env = "" if offline else a.key_env or s.providers.get(a.provider).demo_env
        key = os.environ.get(env, "") if env else ""
        register_secret(key)
        if not offline and not key:
            sys.exit("no API key: set the env var named by --key-env, or use --provider offline")
        prov = None if offline else make_provider(a.provider, s)
        model = a.model or ("" if offline else s.providers.get(a.provider).model)
        pages = run_extraction(imgs, provider=prov, api_key=key, model=model, settings=s,
                               store=store, offline=offline, on_progress=print)
        tables["REG"] = register_table(pages)
    tables["ERP"] = read_table("ERP", a.erp.name, a.erp.read_bytes(), s)
    tables["STORE"] = read_table("STORE", a.store.name, a.store.read_bytes(), s)
    maps = {k: _mapping(t, s, store) for k, t in tables.items()}
    run = reconcile(tables, maps, s, store, pages, a.period_year)
    a.out.mkdir(parents=True, exist_ok=True)
    name = f"reconciliation_{run.run_id}.xlsx"
    (a.out / name).write_bytes(build_workbook(run, s, store.all_decisions()))
    (a.out / "ledger.csv").write_bytes(ledger_csv(run))
    (a.out / "mismatched.csv").write_bytes(mismatched_csv(run, s))
    text = summary_text(run, s, name)
    (a.out / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0 if run.status == "COMPLETE" else 2 if run.complete else 1


if __name__ == "__main__":
    sys.exit(main())
