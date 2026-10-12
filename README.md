# Three-way receiving register reconciliation

Upload photos of a handwritten receiving register, an ERP export and a storekeeper sheet. The tool
reads the register with a vision model, maps every file's columns at runtime and matches the three
sources. It tells you what is matched, mismatched (which field, each source's value) and missing
(from which source). You only get asked about doubtful items. A completeness proof shows every
input row lands in exactly one outcome and that quantity totals tie.

## Run it

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

**Offline demo (no key):** in the sidebar pick *Offline (cached extraction)*. In step 1, click
*Use the bundled demo files*, then *Read register pages* (step 2), *Run reconciliation* (step 3)
and the report (step 4). Headless (both replay shipped cached readings, no key needed):

```bash
python cli.py --register sample_data/synthetic/register_page1.png \
  --erp sample_data/synthetic/erp_export.xlsx --store sample_data/synthetic/store_sheet.csv
python cli.py --register sample_data/t4_register_page1.jpg sample_data/t4_register_page2.jpg \
  --erp sample_data/t4_erp_grn.csv --store sample_data/t4_store_sheet.xlsx --period-year 2026
```

The CLI writes `out/` and exits 0 = complete, 2 = items awaiting review, 1 = incomplete/failed.
To rebuild the demo set, run `python scripts/make_demo_data.py`. Tests: `ruff check . && pytest`.

## Providers and keys

Gemini (default), OpenRouter (`:free` vision models) and Groq, all called over plain REST with
`httpx`. Model IDs live in `config/default.toml` and can be edited in the UI. They are starting
points, and free-tier availability changes. *Test connection* reports auth, quota and
model-not-found problems separately.

- **Demo key:** a server-side env var (`DEMO_GEMINI_API_KEY` etc., see `.env.example`) or a
  Streamlit secret. The browser never sees it. Each session is capped at `app.demo_page_cap`
  pages.
- **Own key:** a password field. It is passed to each provider call as an argument and used only
  for that request's headers. It is never written to disk, SQLite, cache, logs, the report, URLs
  or error messages. Gemini gets it in the `x-goog-api-key` header, never `?key=`. A logging
  filter masks key-like strings and any key seen in-process. *Forget key* clears it.
  `tests/test_app_and_leaks.py` runs a full mocked extraction with a sentinel key and checks that
  the key appears in no output.
- If the app is hosted rather than run locally, your key passes through the host's memory. Use a
  local run for real keys. For the CLI, the key comes from an env var (`--key-env`), never from
  an argument.

## How it works (one module each)

`ingest.py` finds the header row and drops blank, repeated-header and total rows (each one is
recorded). It maps columns to `key, date, supplier, item, lot, qty, unit, ...` using synonym
lists in config, fuzzy header matching and value patterns. You confirm the mapping, and it can be
saved per header fingerprint. `normalize.py` handles dates (detects day/month order, Excel serials,
year taken from the register's period label), quantities as `Decimal` (Indian grouping, negatives
in parentheses), unit aliases with safe conversions, key prefixes and supplier legal suffixes.
Supplier comparison also accepts register shorthand: every token of the shorter name must match
a token of the longer one by prefix or consonant-abbreviation ("pkg" ~ "packaging"), so
"Qasr Pkg" equals "QASR PACKAGING (PVT) LTD".
`extract.py` sends each page to the model twice with different prompts (strict JSON with per-cell
legibility and crossed-out values), makes one repair retry, and caches by image hash + prompt
version + provider + model. `match.py` groups rows by key first. Unkeyed rows only get
*proposals* (score on lot, supplier, item, date ±2 days, quantity) and are never auto-merged.
`ledger.py` gives every row exactly one outcome: `NON_RECEIPT > DUPLICATE_EXTRA > NEEDS_REVIEW >
MISSING > MISMATCHED > MATCHED`. `proof.py` checks the invariants. `review.py` stores decisions
by fingerprint (source + row content + issue). `report.py` builds the workbook and summary.

## Assumptions (all in `config/default.toml` or the sidebar)

- No source of truth by default. You get a "2 of 3 agree" *hint*. Switch with `source_of_truth`.
- Quantity tolerance 0.01 after unit conversion (`tolerances.qty_abs`, `qty_rel`). Date
  tolerance 0 days, with a hint when dates are exactly 1 day apart.
- Units are compared after safe conversions only (e.g. yd→m). Units with no known conversion get
  a "can't compare" review item, not a mismatch.
- Negative quantities are returns/adjustments (`NON_RECEIPT`). They are listed separately with a
  suggested link to a receipt and never netted.
- Entries keyed more than `thresholds.late_entry_days` after the document date, or by an unusual
  user, are flagged. If the entry is in only one source, it goes to review.
- Doubtful items are decided by code, not model confidence: unclear or illegible cells,
  crossed-out values, two readings that disagree, row-count gaps, conflicting duplicates, match
  proposals, returns, suspect entries. Unreadable values are never auto-filled. Other sources'
  values are shown only as hints. Plain mismatches are findings routed to an owner, not
  questions.

## How it fails, and how someone would know

| Failure | Behaviour | You see |
|---|---|---|
| Demo key out of quota / bad key | No retries | Page error naming provider and reason; options: switch, own key, saved readings |
| 429 / 5xx / timeout | Backoff + jitter, honours Retry-After, max 4 tries | "retrying (2/4)" progress, then a final error |
| Invalid JSON from model | One repair retry, then page fails | Run status `INCOMPLETE - page N (...) extraction failed` on screen, in the report and in the summary |
| Missing or wrong columns | Run blocked at mapping | Red "choose a column for qty" message |
| Proof does not tie | Status `FAILED`, never shown as complete | Red proof banner; Proof tab shows FAIL cells |
| Telegram send fails | Retries, then stops | Warning; summary still downloadable. Identical resends refused per run |
| Provider down | Offline mode replays cached readings | Model column says "cached <date>" |

## Data handling

Page images go to the provider you choose. Free tiers may use inputs for training, so use a paid
or private tier for real client data. EXIF/GPS data is stripped before sending. Processed images
stay in `.data/session/<id>/` only for the review screen, and *Delete session data* removes them.
SQLite (`.data/recon.sqlite`) holds the page cache, decisions, mapping profiles and run history,
never keys. Uploaded files are untrusted: size and type limits apply, files are parsed only by
pandas/openpyxl (`data_only`), report cells are protected against formula injection, and model
output is parsed strictly as data.

## Results

- **Synthetic dataset** (`tests/synthetic.py`): renamed columns, no key column in the store,
  dd-mm dates, one line in lb vs kg elsewhere (converted, so it matches). It runs with zero code changes. Proof ties (24/24 rows). Detected: one
  receipt missing from ERP, the ERP quantity outlier (2 of 3 agree), the blotted register
  quantity sent to review (not guessed), a crossed-out quantity, and a backdated ERP-only entry.
- **Kestrel sample (`t4_*`):** register 28 rows on 2 photos, ERP 28, store 30 — proof ties
  (86/86 rows). It runs end to end with no key: the real Gemini readings are shipped as
  cached fixtures, so `--provider offline` replays them. `eval_extraction.py` against the
  hand-verified transcription (`sample_data/ground_truth_register.json`): **28/28 rows
  exact, 223/223 fields, 1/1 illegible cells flagged** (gemini-2.5-flash, prompt v2).
  `tests/test_t4_golden.py` asserts every planted finding is caught by general rules:
  5 mismatches (ERP lot transposition; ERP date one day off; ERP qty outlier on the
  cones line; store still showing the crossed-out 60; store unit mislabel on the fabric
  line — register 1200 yds = ERP 1097.28 m), one receipt missing from ERP, one identical
  store duplicate, one −5000 return linked to its receipt, one ERP-only entry by an
  unusual user sent to review, one sequence gap (4429), and the ink-blotted register qty
  sent to review — read as "2", never guessed. Supplier shorthand in the register
  ("Qasr Pkg", "Zarnab Chem") is matched to the full names, not reported as noise.

## Decisions log

- Unkeyed sources produce one match proposal per row. The review step has a single *Confirm all
  N strong matches* button, which is still a human decision.
- Store-sheet decisions are invalidated when the row's raw content changes, so the question is
  asked again.

## Next week

Bounding-box row crops, `verification = second_provider` UI, an editable config screen, PDF input
hardening, SSO and hosting hardening, a WhatsApp Cloud API sender (swap `notify.py`: same text,
template message via Graph API), audit export and a multi-month dashboard.

## AI tools used

Built with Devin (Cognition) as the coding agent, following `AGENT_PLAN.md` written by Mohib.

## Time log

| Part | Time |
|---|---|
| To be filled in by Mohib with actual times | |
