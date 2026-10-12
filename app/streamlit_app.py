"""Three-way register reconciliation: upload & map -> extract -> review -> report."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recon import notify  # noqa: E402
from recon.config import load_settings  # noqa: E402
from recon.extract import cache_key, duplicate_pages, offline_lookup, run_extraction  # noqa: E402
from recon.imaging import ImageRejected, delete_session_dir, load_all  # noqa: E402
from recon.ingest import (  # noqa: E402
    IngestError,
    auto_map,
    list_sheets,
    missing_required,
    read_table,
    register_table,
)
from recon.logging_utils import register_secret, setup_logging  # noqa: E402
from recon.models import FlagType, Outcome  # noqa: E402
from recon.pipeline import reconcile  # noqa: E402
from recon.providers import PROVIDER_LABELS, make_provider  # noqa: E402
from recon.providers.base import ProviderError  # noqa: E402
from recon.report import build_workbook, ledger_csv, mismatched_csv, summary_text  # noqa: E402
from recon.review import record_decision, strong_proposals  # noqa: E402
from recon.store import Store  # noqa: E402

FIELDS = ["key", "date", "supplier", "item", "lot", "qty", "unit", "entered_by", "entered_at",
          "remarks"]
SS = st.session_state
DEMO_DIR = ROOT / "sample_data" / "synthetic"


class DemoFile:
    """Bundled demo file that looks like a Streamlit upload."""

    def __init__(self, path: Path):
        self.name, self._data = path.name, path.read_bytes()
        self.size = len(self._data)

    def getvalue(self) -> bytes:
        """File bytes."""
        return self._data


def demo_files() -> tuple[list, object, object]:
    """Register pages, ERP and store files from the bundled demo set."""
    imgs = [DemoFile(p) for p in sorted(DEMO_DIR.glob("register_*"))]
    erp = next(DEMO_DIR.glob("erp_*"), None)
    sto = next(DEMO_DIR.glob("store_*"), None)
    return imgs, erp and DemoFile(erp), sto and DemoFile(sto)


def settings():
    """Config with the live sidebar overrides."""
    return load_settings(overrides={"matching": {"source_of_truth": SS.get("sot", "none")}})


def data_dir() -> Path:
    """Data folder: RECON_DATA_DIR if set (tests, hosting), else config app.data_dir."""
    return Path(os.environ.get("RECON_DATA_DIR") or ROOT / load_settings().app.data_dir)


def store() -> Store:
    """SQLite store (cache, decisions, mappings). Holds no keys."""
    return Store(data_dir() / "recon.sqlite")


def session_dir() -> Path:
    """Per-session folder for processed page images."""
    SS.setdefault("sid", uuid.uuid4().hex[:12])
    return data_dir() / "session" / SS.sid


def demo_key(provider: str) -> str:
    """Server-side demo key from env or Streamlit secrets; never sent to the browser."""
    env = load_settings().providers.get(provider).demo_env
    val = os.environ.get(env, "")
    if not val:
        try:
            val = st.secrets.get(env, "")
        except Exception:
            val = ""
    return val


def current_key() -> str:
    """Key for this request only; empty in offline mode."""
    p = SS.get("provider", "gemini")
    if p == "offline":
        return ""
    key = demo_key(p) if SS.get("key_source") == "Demo key" else SS.get("user_key", "")
    register_secret(key)
    return key


def forget_key() -> None:
    """Clear the pasted key from widget state."""
    SS["user_key"] = ""


def use_offline() -> None:
    """Switch to cached extraction (failure fallback)."""
    SS["provider"] = "offline"


# ---------------- sidebar ----------------

def sidebar() -> None:
    """Provider, key, model and run settings."""
    s = load_settings()
    sb = st.sidebar
    sb.header("Settings")
    names = ["gemini", "openrouter", "groq", "offline"]
    SS.setdefault("provider", s.providers.default)
    p = sb.selectbox("Reader (vision model)", names, key="provider",
                     format_func=lambda n: PROVIDER_LABELS[n])
    if p != "offline":
        cfg = s.providers.get(p)
        SS.setdefault(f"model_{p}", cfg.model)
        sb.text_input("Model", key=f"model_{p}",
                      help="Starting points; free-tier availability may change.")
        has_demo = bool(demo_key(p))
        opts = (["Demo key"] if has_demo else []) + ["My own key (not stored)"]
        sb.radio("Key", opts, key="key_source")
        if has_demo and SS.get("key_source") == "Demo key":
            sb.caption(f"Demo key available ✓ (max {s.app.demo_page_cap} pages per session)")
        else:
            sb.text_input("API key", type="password", key="user_key")
            sb.caption("Used only for this session's requests, sent only to the provider you "
                       "picked, never written to disk or logs.")
            sb.button("Forget key", on_click=forget_key)
        c1, c2 = sb.columns(2)
        if c1.button("Test connection"):
            test_connection(p)
        if c2.button("Refresh models"):
            refresh_models(p)
        sb.selectbox("Second reading", ["second_pass", "off"], key="verification",
                     help="Read each page twice with different prompts and flag disagreements.")
    else:
        sb.info("Uses saved readings of pages already seen (no key, no upload).")
    sb.number_input("Register year (0 = from page)", 0, 2100, 0, key="period_year")
    sb.selectbox("Source of truth", ["none", "REG", "ERP", "STORE"], key="sot",
                 help="'none' reports disagreements with a majority hint only.")
    sb.text_input("Reviewer name", key="reviewer")
    if sb.button("Delete session data"):
        delete_session_dir(session_dir())
        for k in list(SS.keys()):
            del SS[k]
        st.rerun()


def test_connection(p: str) -> None:
    """Tiny call that distinguishes auth / quota / model problems."""
    key = current_key()
    if not key:
        st.sidebar.error("Enter a key first.")
        return
    try:
        make_provider(p, load_settings()).test_connection(
            api_key=key, model=SS[f"model_{p}"], timeout=30)
        st.sidebar.success(f"{PROVIDER_LABELS[p]} accepted the key and model.")
    except ProviderError as e:
        st.sidebar.error(e.user_message())


def refresh_models(p: str) -> None:
    """Best-effort list of vision models from the provider."""
    try:
        models = make_provider(p, load_settings()).list_models(api_key=current_key(), timeout=30)
        st.sidebar.write(models[:30] or "No models returned.")
    except ProviderError as e:
        st.sidebar.error(e.user_message())


# ---------------- step 1: upload & map ----------------

def mapping_editor(src: str, table) -> dict | None:
    """Confirm or change the detected column for each field; None until required ones exist."""
    s, db = settings(), store()
    k = f"map_{src}_{table.fingerprint}"
    if k not in SS:
        SS[k] = db.get_mapping(table.fingerprint) or auto_map(table, s)
    m = SS[k]
    cols = [None, *table.columns]
    sample = pd.DataFrame([r for _, r in table.records[:3]])
    with st.expander(f"Columns for {table.name}", expanded=bool(missing_required(m, s))):
        st.dataframe(sample, hide_index=True)
        grid = st.columns(5)
        for i, f in enumerate(FIELDS):
            cur = m.get(f)
            m[f] = grid[i % 5].selectbox(f, cols, index=cols.index(cur) if cur in cols else 0,
                                         key=f"{k}_{f}")
        if not m.get("key"):
            st.caption("No receipt-number column: rows will be matched on lot, supplier, item, "
                       "date and quantity, and you'll be asked to confirm.")
        if st.button("Save as default for files like this", key=f"{k}_save"):
            db.put_mapping(table.fingerprint, src, m)
            st.success("Saved. Next time these columns are pre-filled.")
    miss = missing_required(m, s)
    if miss:
        st.error(f"{table.name}: choose a column for {', '.join(miss)} before running.")
        return None
    return m


def read_upload(src: str, f) -> object | None:
    """Read a CSV/XLSX upload into a Table, asking for a sheet if needed."""
    data = f.getvalue()
    s = settings()
    if len(data) > s.app.max_file_mb * 1024 * 1024:
        st.error(f"{f.name} is larger than {s.app.max_file_mb} MB.")
        return None
    sheet = None
    if f.name.lower().endswith(".xlsx"):
        sheets = list_sheets(data)
        if len(sheets) > 1:
            sheet = st.selectbox(f"Sheet in {f.name}", sheets, key=f"sheet_{src}")
    try:
        return read_table(src, f.name, data, s, sheet)
    except IngestError as e:
        st.error(f"{f.name}: {e}")
        return None


def step_upload() -> None:
    """Upload the three sources and confirm column mappings."""
    s = settings()
    imgs = st.file_uploader("Register page photos", accept_multiple_files=True,
                            type=["jpg", "jpeg", "png", "webp", "pdf"])
    c1, c2 = st.columns(2)
    erp = c1.file_uploader("ERP export (CSV/XLSX)", type=["csv", "xlsx"])
    sto = c2.file_uploader("Storekeeper sheet (CSV/XLSX)", type=["csv", "xlsx"])
    if DEMO_DIR.exists() and st.button("Use the bundled demo files (works offline, no key)"):
        SS.demo = True
    if SS.get("demo") and not (imgs or erp or sto):
        imgs, erp, sto = demo_files()
        st.info("Using bundled demo files: " + ", ".join(f.name for f in [*imgs, erp, sto] if f))
    if imgs:
        sig = tuple((f.name, f.size) for f in imgs)
        if SS.get("img_sig") != sig:
            try:
                SS.pages = load_all([(f.name, f.getvalue()) for f in imgs], s)
                SS.img_sig, SS.page_results = sig, None
            except ImageRejected as e:
                st.error(str(e))
                SS.pages = []
        for w in duplicate_pages(SS.get("pages", [])):
            st.warning(w)
        st.caption(cache_status(SS.get("pages", [])))
    tables = {}
    for src, f in (("ERP", erp), ("STORE", sto)):
        if f:
            t = read_upload(src, f)
            if t:
                tables[src] = t
                st.caption(f"{t.name}: {t.rows_read} rows read, {len(t.records)} kept, "
                           f"{len(t.dropped)} set aside (blank/header/total rows)")
    SS.maps = {src: mapping_editor(src, t) for src, t in tables.items()}
    SS.tables = tables


def cache_status(pages) -> str:
    """'3 pages cached, 1 new' for the selected reader."""
    if not pages:
        return ""
    p, db, s = SS.get("provider"), store(), settings()
    if p == "offline":
        n = sum(1 for pg in pages if offline_lookup(pg, db))
    else:
        model = SS.get(f"model_{p}", s.providers.get(p).model)
        n = sum(1 for pg in pages if db.get_page(cache_key(
            pg.sha256, s.extraction.prompt_version, p, model, "A")))
    return f"{len(pages)} register page(s): {n} already read (cached), {len(pages) - n} new"


# ---------------- step 2: extract ----------------

def step_extract() -> None:
    """Read register pages with the chosen model; show doubtful cells and failures."""
    pages = SS.get("pages") or []
    if not pages:
        st.info("Upload register photos in step 1 (or skip the register).")
        return
    p = SS.get("provider")
    if p != "offline":
        st.caption(f"Page images will be sent to {PROVIDER_LABELS[p]}. Free tiers may use inputs "
                   "to improve models; use a paid/private tier for real client data.")
    if st.button("Read register pages", type="primary"):
        extract(pages)
    res = SS.get("page_results")
    if not res:
        return
    failed = [r for r in res if r.status == "failed"]
    for r in failed:
        st.error(f"Page {r.page_index + 1} ({r.file_name}) could not be read: {r.error}")
    if failed:
        st.warning("What to do: switch reader in the sidebar, paste your own key, or use saved "
                   "readings. The run will be marked INCOMPLETE until every page is read.")
        st.button("Use saved readings (offline)", on_click=use_offline)
    ok = [r for r in res if r.status == "ok"]
    if ok:
        table = register_table(res)
        show_register(table)
        SS.reg_table = table
        SS.reg_map = mapping_editor("REG", table)


def extract(pages) -> None:
    """Run extraction with progress; enforce the demo page cap."""
    s, p = settings(), SS.get("provider")
    offline = p == "offline"
    key = current_key()
    if not offline and not key:
        st.error("No key: choose 'Demo key', paste your own, or pick Offline.")
        return
    if not offline and SS.get("key_source") == "Demo key":
        used = SS.get("demo_pages_used", 0)
        if used + len(pages) > s.app.demo_page_cap:
            st.error(f"The shared demo key is limited to {s.app.demo_page_cap} pages per session. "
                     "Paste your own key or use saved readings.")
            return
    model = "" if offline else SS.get(f"model_{p}")
    with st.status("Reading pages...", expanded=True) as box:
        res = run_extraction(
            pages, provider=None if offline else make_provider(p, s), api_key=key, model=model,
            settings=s, store=store(), verification=SS.get("verification", "second_pass"),
            offline=offline, on_progress=box.write)
        box.update(label="Done reading pages", state="complete")
    if not offline and SS.get("key_source") == "Demo key":
        SS.demo_pages_used = SS.get("demo_pages_used", 0) + sum(not r.cached for r in res)
    SS.page_results, SS.run = res, None


def show_register(table) -> None:
    """Register rows with doubtful cells highlighted."""
    df = pd.DataFrame([r for _, r in table.records], index=[ref for ref, _ in table.records])
    cells = table.meta.get("cells", {})

    def style(row):
        out = []
        for col in df.columns:
            c = cells.get(row.name, {}).get(col)
            doubtful = c is not None and (c.legibility != "clear" or c.struck_text)
            out.append("background-color: #ffe08a" if doubtful else "")
        return out
    st.dataframe(df.style.apply(style, axis=1))
    st.caption("Yellow = doubtful cell (unclear or crossed out). These go to the review step.")


# ---------------- step 3: review ----------------

def inputs_ready() -> tuple[dict, dict] | None:
    """Tables and confirmed mappings, or None with a message."""
    tables, maps = dict(SS.get("tables") or {}), dict(SS.get("maps") or {})
    if SS.get("reg_table") is not None:
        tables["REG"], maps["REG"] = SS.reg_table, SS.get("reg_map")
    if len(tables) < 2:
        st.info("Add at least two sources in steps 1–2.")
        return None
    if any(maps.get(k) is None for k in tables):
        st.error("Some columns still need mapping (step 1/2).")
        return None
    order = [s for s in settings().sources.order if s in tables]
    return {k: tables[k] for k in order}, {k: maps[k] for k in order}


def run_now() -> None:
    """(Re)build the run from inputs + stored decisions."""
    ready = inputs_ready()
    if ready:
        year = SS.get("period_year") or None
        SS.run = reconcile(ready[0], ready[1], settings(), store(), SS.get("page_results"), year)


def decide(item, action: str, value: str | None = None) -> None:
    """Persist a decision and recompute."""
    record_decision(store(), item, action, value, SS.get("reviewer") or "unknown")
    run_now()


def step_review() -> None:
    """Only doubtful items, highest impact first."""
    if st.button("Run reconciliation", type="primary") or (SS.get("run") is None and
                                                          inputs_ready()):
        run_now()
    run = SS.get("run")
    if run is None:
        return
    q = run.queue
    total = SS.setdefault("queue_start", {}).setdefault(run.inputs[0].sha256, len(q))
    st.progress(1 - len(q) / max(total, 1), text=f"{max(total - len(q), 0)} of {total} resolved")
    if not q:
        st.success("Nothing needs a decision. Go to the report.")
        return
    strong = strong_proposals(q, settings().matching.tier2_min_score)
    if len(strong) > 1 and st.button(f"Confirm all {len(strong)} strong matches"):
        db, who = store(), SS.get("reviewer") or "unknown"
        for it in strong:
            record_decision(db, it, "confirm_match", it.suggestion, who)
        run_now()
        st.rerun()
    rows = {r.row_id: r for r in run.rows}
    for it in q[:15]:
        card(it, rows.get(it.row_id), run)
    if len(q) > 15:
        st.caption(f"{len(q) - 15} more after these.")


def source_table(it, row, run) -> pd.DataFrame:
    """What this row says, and what other sources say (hints only)."""
    data = {}
    if row is not None:
        data[f"{row.source} (this row)"] = {
            "key": row.key_raw, "date": row.date_raw, "supplier": row.supplier_raw,
            "item": row.item_raw, "lot": row.extras.get("lot_raw"),
            "qty": None if row.qty_raw is None else str(row.qty_raw), "unit": row.unit_raw}
    for src, v in it.hints.items():
        data[f"{src} (hint)"] = {it.field: v}
    return pd.DataFrame(data)


def card(it, row, run) -> None:
    """One review card with the buttons that fit its type."""
    k = it.fingerprint
    with st.container(border=True):
        st.markdown(f"**{it.question}**  \n{it.row_id}: {it.message}")
        page_image(it)
        if row is not None or it.hints:
            st.dataframe(source_table(it, row, run))
        t = it.type
        c1, c2, c3 = st.columns(3)
        if t in (FlagType.UNCLEAR_FIELD, FlagType.BAD_FORMAT, FlagType.UNIT_INCOMPARABLE):
            v = c1.text_input("Correct value", key=f"v{k}",
                              placeholder="e.g. 144 cone" if t == FlagType.UNIT_INCOMPARABLE
                              else "type what the page says")
            if c2.button("Save value", key=f"s{k}") and v:
                decide(it, "enter_value", v)
                st.rerun()
        elif t in (FlagType.STRUCK_VALUE, FlagType.PASS_DISAGREE) and it.candidates:
            v = c1.radio("Final value", it.candidates, key=f"v{k}")
            if c2.button("Use this", key=f"s{k}"):
                decide(it, "accept_value", v)
                st.rerun()
        elif t == FlagType.FUZZY_MATCH_PROPOSAL:
            if c1.button("Same receipt", key=f"y{k}"):
                decide(it, "confirm_match", it.suggestion)
                st.rerun()
            if c2.button("Different", key=f"n{k}"):
                decide(it, "reject_match", it.suggestion)
                st.rerun()
        elif t == FlagType.CONFLICTING_DUPLICATE:
            if c1.button("This row is the extra copy", key=f"y{k}"):
                decide(it, "mark_duplicate")
                st.rerun()
        elif t == FlagType.NON_RECEIPT_LINK and it.suggestion:
            if c1.button(f"Link to {it.suggestion}", key=f"y{k}"):
                decide(it, "link_non_receipt", it.suggestion)
                st.rerun()
        exc = "Legitimate" if t == FlagType.SUSPECT_ENTRY else "Accept as exception"
        if t in (FlagType.ROW_COUNT_MISMATCH, FlagType.PASS_DISAGREE) and not it.candidates:
            exc = "All rows are captured"
        if c3.button(exc, key=f"x{k}"):
            decide(it, "accept_as_exception", exc)
            st.rerun()
        if c3.button("Later", key=f"d{k}"):
            decide(it, "defer")
            st.rerun()


def page_image(it) -> None:
    """Whole register page for register items (row number in the card title)."""
    if it.source != "REG" or not it.page:
        return
    pages = SS.get("pages") or []
    if 0 < it.page <= len(pages):
        with st.expander(f"Page {it.page} image"):
            st.image(pages[it.page - 1].jpeg)


# ---------------- step 4: report ----------------

def step_report() -> None:
    """Status, proof, exceptions, downloads, summary."""
    run = SS.get("run")
    if run is None:
        st.info("Run the reconciliation in step 3 first.")
        return
    s = settings()
    badge = st.success if run.status == "COMPLETE" else st.warning if run.complete else st.error
    badge(run.status)
    counts = {o.value: sum(e.outcome == o for e in run.ledger) for o in Outcome}
    for col, (name, n) in zip(st.columns(len(counts)), counts.items(), strict=True):
        col.metric(name.replace("_", " ").title(), n)
    if run.proof.ok:
        st.success(f"Proof: all {len(run.ledger)} rows accounted for, quantity totals tie.")
    else:
        st.error("Proof FAILED. Do not rely on these numbers.")
    with st.expander("Proof details"):
        st.dataframe(pd.DataFrame([c.__dict__ for c in run.proof.checks]), hide_index=True)
    name = f"reconciliation_{run.run_id}{'' if run.complete else '_INCOMPLETE'}.xlsx"
    text = summary_text(run, s, name)
    st.subheader("Summary")
    st.code(text, language=None)
    trace(run)
    db = store()
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("Excel report", build_workbook(run, s, db.all_decisions()), name)
    c2.download_button("Ledger CSV", ledger_csv(run), "ledger.csv")
    c3.download_button("Mismatched CSV", mismatched_csv(run, s), "mismatched.csv")
    c4.download_button("Summary .txt", text.encode(), "summary.txt")
    telegram(text, run)


def trace(run) -> None:
    """Pick any receipt and see the exact source rows behind it."""
    groups = [g for g in run.match.groups if g.mismatches or len(g.sources) < len(run.inputs)]
    if not groups:
        return
    labels = {f"{g.key or g.group_id} – {', '.join(m.field for m in g.mismatches) or 'missing'}":
              g for g in groups}
    pick = st.selectbox("Trace an exception to its source rows", list(labels))
    g = labels[pick]
    st.dataframe(pd.DataFrame([{"row": r.row_id, **r.raw} for rs in g.members.values()
                               for r in rs]), hide_index=True)
    for h in g.hints:
        st.caption(h)


def telegram(text: str, run) -> None:
    """Optional send; token never stored."""
    with st.expander("Send summary to Telegram (optional)"):
        tok = st.text_input("Bot token", type="password", key="tg_token")
        chat = st.text_input("Chat id", key="tg_chat")
        force = st.checkbox("Send again even if already sent", key="tg_force")
        if st.button("Send") and tok and chat:
            try:
                notify.send_telegram(text, token=tok, chat_id=chat, run_id=run.run_id,
                                     store=store(), force=force)
                st.success("Sent.")
            except notify.AlreadySent as e:
                st.warning(f"{e}. Tick 'send again' to resend.")
            except notify.NotifyError as e:
                st.error(f"{e}. The summary is still available to copy or download above.")


def main() -> None:
    """Page layout."""
    st.set_page_config(page_title="Register reconciliation", layout="wide")
    setup_logging()
    st.title("Receiving register reconciliation")
    st.caption("Register photos vs ERP vs store sheet. Exceptions first; only doubtful items "
               "are asked.")
    sidebar()
    t1, t2, t3, t4 = st.tabs(["1. Upload & map", "2. Read register", "3. Review doubtful items",
                              "4. Report"])
    with t1:
        step_upload()
    with t2:
        step_extract()
    with t3:
        step_review()
    with t4:
        step_report()


main()
