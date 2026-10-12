"""Normalisation of quantities, dates, units, keys, suppliers and items. Config-driven."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

from dateutil import parser as dparser

from recon.config import Settings

# ---------- quantities ----------

_NUM_RE = re.compile(r"^\(?\s*[-+]?\s*\(?\s*[\d.,\s]+\)?\s*-?$")


def parse_qty(raw: object) -> tuple[Decimal | None, str | None]:
    """Parse a signed quantity; return (value, trailing unit text). Handles 1,00,000 and (5)."""
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, None
    if isinstance(raw, int | Decimal):
        return Decimal(raw), None
    if isinstance(raw, float):
        return (None, None) if raw != raw else (Decimal(str(raw)).normalize() + 0, None)
    s = unicodedata.normalize("NFKC", str(raw)).strip().replace("\u2212", "-")
    if not s:
        return None, None
    m = re.match(r"^([-+()\d.,\s]+?)\s*([A-Za-z][A-Za-z.\s]*)?$", s)
    if not m:
        return None, None
    num, unit = m.group(1).strip(), (m.group(2) or "").strip() or None
    neg = num.startswith("(") and num.endswith(")") or num.startswith("-") or num.endswith("-")
    digits = re.sub(r"[()\s+\-]", "", num)
    if not digits or not _NUM_RE.match(num):
        return None, unit
    if digits.count(".") > 1:
        return None, unit
    digits = digits.replace(",", "")
    try:
        val = Decimal(digits)
    except InvalidOperation:
        return None, unit
    return (-val if neg else val), unit


# ---------- dates ----------

_DMY = re.compile(r"^\s*(\d{1,4})[/\-. ](\d{1,2})(?:[/\-. ](\d{2,4}))?\s*$")


def detect_dayfirst(values: list[object]) -> bool | None:
    """Infer day-first vs month-first from unambiguous text dates (a part > 12)."""
    votes = Counter()
    for v in values:
        if not isinstance(v, str):
            continue
        m = _DMY.match(v)
        if not m or len(m.group(1)) == 4:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            votes["day"] += 1
        elif b > 12 >= a:
            votes["month"] += 1
    if not votes:
        return None
    return votes["day"] >= votes["month"]


def is_ambiguous_date(v: object) -> bool:
    """True for text like 03/04/2026 where both orders are valid and differ."""
    if not isinstance(v, str):
        return False
    m = _DMY.match(v)
    if not m or len(m.group(1)) == 4:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a <= 12 and b <= 12 and a != b


def excel_serial_to_date(n: float) -> dt.date:
    """Convert an Excel serial day number to a date."""
    return dt.date(1899, 12, 30) + dt.timedelta(days=int(n))


def parse_date(value: object, dayfirst: bool, default_year: int | None = None) -> dt.date | None:
    """Parse a date from datetime, Excel serial, ISO or free text; None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, int | float) and 20000 < float(value) < 80000:
        return excel_serial_to_date(float(value))
    s = str(value).strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", s):
        dayfirst = False
    year = default_year or dt.date.today().year
    try:
        d = dparser.parse(s, dayfirst=dayfirst, default=dt.datetime(year, 1, 1), fuzzy=False)
    except (ValueError, OverflowError):
        return None
    return d.date()


def parse_datetime(value: object, dayfirst: bool) -> dt.datetime | None:
    """Parse an entry timestamp (datetime or text)."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    try:
        return dparser.parse(str(value), dayfirst=dayfirst)
    except (ValueError, OverflowError):
        return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def year_from_label(label: str | None) -> int | None:
    """Pull a 4-digit (or 'Sep 26' style 2-digit) year out of a period label."""
    if not label:
        return None
    m = re.search(r"(19|20)\d{2}", label)
    if m:
        return int(m.group(0))
    m = re.search(r"[A-Za-z]{3,}\W*'?(\d{2})\b", label)
    return 2000 + int(m.group(1)) if m else None


# ---------- units ----------

def _unit_token(raw: str) -> str:
    return re.sub(r"[.\s]", "", raw.strip().casefold())


def canonical_unit(raw: object, settings: Settings,
                   learned: dict[str, str] | None = None) -> str | None:
    """Map a unit alias to its canonical name, or None if unknown."""
    if raw is None or str(raw).strip() == "":
        return None
    tok = _unit_token(str(raw))
    if learned and tok in learned:
        return learned[tok]
    for canon, aliases in settings.units.aliases.items():
        if tok == canon or tok in (_unit_token(a) for a in aliases):
            return canon
    return None


def to_base(qty: Decimal | None, unit: str | None,
            settings: Settings) -> tuple[Decimal | None, str | None]:
    """Convert to the base unit using only configured safe conversions."""
    if qty is None or unit is None:
        return qty, unit
    seen = {unit}
    cur_q, cur_u = qty, unit
    while True:
        conv = next((c for c in settings.units.conversions if c.from_ == cur_u), None)
        if not conv or conv.to in seen:
            return cur_q, cur_u
        cur_q, cur_u = cur_q * conv.factor, conv.to
        seen.add(cur_u)


def base_unit_of(unit: str | None, settings: Settings) -> str | None:
    """Base unit a canonical unit converts to."""
    return to_base(Decimal(1), unit, settings)[1] if unit else None


# ---------- keys ----------

def _alnum(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s).upper()


def common_prefix(values: list[str]) -> str:
    """Alphabetic prefix shared by most non-empty key values in a column (e.g. 'GRN')."""
    prefixes = Counter(re.match(r"^[A-Z]*", _alnum(v)).group(0) for v in values if v)
    if not prefixes:
        return ""
    pref, n = prefixes.most_common(1)[0]
    return pref if pref and n >= max(1, 0.6 * sum(prefixes.values())) else ""


def normalize_key(raw: object, prefix: str = "") -> str | None:
    """Strip separators, the column's shared prefix, and leading zeros."""
    if raw is None:
        return None
    s = _alnum(str(raw).removesuffix(".0") if isinstance(raw, float) else str(raw))
    if not s:
        return None
    if prefix and s.startswith(prefix) and len(s) > len(prefix):
        s = s[len(prefix):]
    m = re.match(r"^([A-Z]*)(\d+)$", s)
    if m:
        return m.group(1) + str(int(m.group(2)))
    return s


# ---------- text ----------

def normalize_supplier(raw: object, settings: Settings) -> str | None:
    """Casefold, drop punctuation and legal suffixes, collapse whitespace."""
    if raw is None or str(raw).strip() == "":
        return None
    s = unicodedata.normalize("NFKC", str(raw)).casefold().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    drop = {x.casefold() for x in settings.normalize.legal_suffixes}
    toks = [t for t in s.split() if t not in drop]
    return " ".join(toks) or s.strip()


def normalize_item(raw: object) -> str | None:
    """Casefold and collapse whitespace/punctuation."""
    if raw is None or str(raw).strip() == "":
        return None
    s = unicodedata.normalize("NFKC", str(raw)).casefold()
    return " ".join(re.sub(r"[^\w\s./-]", " ", s).split())


def ocr_fold(s: str | None, settings: Settings) -> str:
    """Comparison-only form treating common OCR confusions as equal."""
    if not s:
        return ""
    out = s.casefold()
    for a, b in settings.normalize.ocr_confusions:
        out = out.replace(a.casefold(), b.casefold())
    return re.sub(r"[\s\-_/.]", "", out)


def normalize_lot(raw: object) -> str | None:
    """Uppercase alphanumerics only, for comparison."""
    if raw is None:
        return None
    s = _alnum(str(raw))
    return s or None
