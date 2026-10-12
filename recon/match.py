"""Tiered matching: key groups (Tier 1), composite proposals (Tier 2), field comparison."""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from recon.config import Settings
from recon.models import Flag, FlagType, SourceRow
from recon.normalize import ocr_fold


@dataclass
class FieldMismatch:
    """One field that differs inside a group."""

    field: str
    values: dict[str, str]                 # source -> displayed value (raw + converted)
    hint: str = ""                         # majority / source-of-truth hint
    outliers: list[str] = field(default_factory=list)


@dataclass
class Group:
    """Rows from different sources believed to be the same receipt."""

    group_id: str
    key: str | None
    members: dict[str, list[SourceRow]] = field(default_factory=dict)   # first = primary
    duplicates: list[SourceRow] = field(default_factory=list)
    tier: int = 1
    mismatches: list[FieldMismatch] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)                 # row_ids proposed in

    def primary(self, source: str) -> SourceRow | None:
        """First row from a source, if any."""
        rows = self.members.get(source) or []
        return rows[0] if rows else None

    @property
    def sources(self) -> list[str]:
        """Sources present in this group."""
        return [s for s, rows in self.members.items() if rows]


@dataclass
class MatchResult:
    """Output of matching."""

    groups: list[Group]
    non_receipts: list[SourceRow]
    duplicates: set[str]                   # row_ids that are DUPLICATE_EXTRA
    group_of: dict[str, str]               # row_id -> group_id
    sequence_gaps: list[str]


def _sig(r: SourceRow) -> tuple:
    return (r.date, r.supplier_norm, r.item_norm, r.lot, r.qty_base, r.base_unit)


def is_transposition(a: str | None, b: str | None) -> bool:
    """True if a and b differ by one adjacent swap or one edit."""
    if not a or not b or a == b:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] \
                and a[diff[1]] == b[diff[0]]:
            return True
    return Levenshtein.distance(a, b) == 1


def is_non_receipt(r: SourceRow) -> bool:
    """Negative quantities are returns/write-offs, not receipts."""
    return r.qty is not None and r.qty < 0


# ---------- Tier 2 scoring ----------

def pair_score(r: SourceRow, g: Group, s: Settings) -> float:
    """0..1 similarity of a row to a group's primaries (lot, supplier, item, date, qty, key)."""
    best = 0.0
    for src in g.sources:
        o = g.primary(src)
        sc = 0.0
        if r.lot and o.lot and r.lot == o.lot:
            sc += 0.35
        if r.supplier_norm and o.supplier_norm and fuzz.token_set_ratio(
                r.supplier_norm, o.supplier_norm) >= s.matching.supplier_threshold:
            sc += 0.15
        if r.item_norm and o.item_norm and fuzz.token_set_ratio(
                ocr_fold(r.item_norm, s), ocr_fold(o.item_norm, s)) >= s.matching.item_threshold:
            sc += 0.2
        if r.date and o.date and abs((r.date - o.date).days) <= s.matching.tier2_date_window_days:
            sc += 0.15
        if r.qty_base is not None and o.qty_base is not None and r.base_unit == o.base_unit \
                and qty_equal(r.qty_base, o.qty_base, s):
            sc += 0.15
        if r.key and o.key and is_transposition(r.key, o.key):
            sc += 0.15
        best = max(best, min(sc, 1.0))
    return best


def qty_equal(a: Decimal, b: Decimal, s: Settings) -> bool:
    """Equal within absolute or relative tolerance."""
    diff = abs(a - b)
    if diff <= s.tolerances.qty_abs:
        return True
    return s.tolerances.qty_rel > 0 and diff <= s.tolerances.qty_rel * max(abs(a), abs(b))


# ---------- grouping ----------

def build_groups(rows: list[SourceRow], s: Settings, confirmed: dict[str, str] | None = None,
                 rejected: set[tuple[str, str]] | None = None) -> MatchResult:
    """Group rows by key, then propose (never auto-merge) composite matches."""
    confirmed, rejected = confirmed or {}, rejected or set()
    non_receipts = [r for r in rows if is_non_receipt(r)]
    nr_ids = {r.row_id for r in non_receipts}
    receipts = [r for r in rows if r.row_id not in nr_ids]
    groups: dict[str, Group] = {}
    loose: list[SourceRow] = []
    for r in receipts:
        if r.row_id in confirmed or not r.key:
            loose.append(r)
            continue
        gid = f"K:{r.key}"
        g = groups.setdefault(gid, Group(gid, r.key))
        g.members.setdefault(r.source, []).append(r)
    for r in loose:
        gid = f"U:{r.row_id}"
        groups[gid] = Group(gid, r.key, {r.source: [r]}, tier=2)
    # apply confirmed Tier-2 matches (reviewer said "same receipt")
    for row_id, target in confirmed.items():
        src_gid = next((gid for gid, g in groups.items()
                        if any(x.row_id == row_id for rs in g.members.values() for x in rs)), None)
        if src_gid and target in groups and src_gid != target and src_gid.startswith("U:"):
            g = groups.pop(src_gid)
            for src, rs in g.members.items():
                groups[target].members.setdefault(src, []).extend(rs)
            groups[target].tier = 2
    duplicates = _split_duplicates(groups.values())
    _propose(groups, s, rejected)
    group_of = {r.row_id: g.group_id for g in groups.values() for rs in g.members.values()
                for r in rs}
    for nr in non_receipts:
        _suggest_link(nr, groups, s)
    return MatchResult(list(groups.values()), non_receipts, duplicates, group_of,
                       sequence_gaps(rows))


def _split_duplicates(groups) -> set[str]:
    """Identical repeats -> DUPLICATE_EXTRA; differing repeats -> CONFLICTING_DUPLICATE."""
    dupes: set[str] = set()
    for g in groups:
        for src, rs in g.members.items():
            if len(rs) < 2:
                continue
            first, rest = rs[0], rs[1:]
            if all(_sig(x) == _sig(first) for x in rest):
                g.duplicates.extend(rest)
                dupes.update(x.row_id for x in rest)
                g.members[src] = [first]
                g.hints.append(f"{src} has {len(rest)} identical repeat(s) of this receipt")
            else:
                for x in rs:
                    x.flags.append(Flag(
                        type=FlagType.CONFLICTING_DUPLICATE, field=None,
                        candidates=[y.row_id for y in rs],
                        message=f"key {g.key} appears {len(rs)} times in {src} with different "
                                "content; which is right?"))
    return dupes


def _propose(groups: dict[str, Group], s: Settings, rejected: set[tuple[str, str]]) -> None:
    """Single-source groups get a FUZZY_MATCH_PROPOSAL if a strong unique candidate exists."""
    singles = [g for g in groups.values() if len(g.sources) == 1]
    for g in singles:
        src = g.sources[0]
        r = g.primary(src)
        cands = []
        for other in groups.values():
            if other is g or src in other.sources or (r.row_id, other.group_id) in rejected:
                continue
            cands.append((pair_score(r, other, s), other))
        cands.sort(key=lambda x: -x[0])
        if not cands:
            continue
        best, target = cands[0]
        runner = cands[1][0] if len(cands) > 1 else 0.0
        if best >= s.matching.tier2_min_score and best - runner >= s.matching.tier2_margin:
            if target.key and r.key and is_transposition(r.key, target.key):
                g.hints.append(f"TRANSPOSITION_HINT: key {r.key} vs {target.key}")
            for x in g.members[src]:
                x.flags.append(Flag(
                    type=FlagType.FUZZY_MATCH_PROPOSAL, candidates=[target.group_id],
                    suggestion=target.group_id,
                    message=f"looks like the same receipt as {_label(target)} "
                            f"(score {best:.2f}); confirm?"))
            target.proposals.append(r.row_id)


def _label(g: Group) -> str:
    if g.key:
        return f"key {g.key}"
    r = g.primary(g.sources[0])
    return f"{r.row_id} ({r.item_raw or ''} {r.qty_raw or ''})".strip()


def _suggest_link(nr: SourceRow, groups: dict[str, Group], s: Settings) -> None:
    """Suggest which receipt a return/adjustment belongs to (by lot, supplier, item)."""
    probe = nr.model_copy(update={"qty_base": None, "date": None, "key": None})
    scored = sorted(((pair_score(probe, g, s), g) for g in groups.values()), key=lambda x: -x[0])
    best = scored[0] if scored and scored[0][0] >= 0.35 else None
    nr.flags.append(Flag(
        type=FlagType.NON_RECEIPT_LINK, blocking=False,
        candidates=[best[1].group_id] if best else [],
        suggestion=best[1].group_id if best else None,
        message="return/adjustment (negative quantity); "
                + (f"likely relates to {_label(best[1])}" if best else "no obvious receipt")))


def sequence_gaps(rows: list[SourceRow]) -> list[str]:
    """Numeric keys missing from the observed range in every source."""
    nums = sorted({int(r.key) for r in rows if r.key and r.key.isdigit()})
    if len(nums) < 3 or nums[-1] - nums[0] > 5 * len(nums):
        return []
    have = set(nums)
    return [str(n) for n in range(nums[0], nums[-1] + 1) if n not in have]


# ---------- comparison ----------

def _fmt_qty(r: SourceRow) -> str:
    raw = f"{r.qty if r.qty is not None else r.qty_raw} {r.unit_raw or r.unit or ''}".strip()
    if r.unit and r.base_unit and r.unit != r.base_unit and r.qty_base is not None:
        return f"{raw} (= {r.qty_base.normalize():f} {r.base_unit})"
    return raw


def _display(r: SourceRow, f: str) -> str:
    return {"date": lambda: r.date.isoformat() if r.date else (r.date_raw or ""),
            "supplier": lambda: r.supplier_raw or "", "item": lambda: r.item_raw or "",
            "lot": lambda: r.extras.get("lot_raw") or r.lot or "",
            "qty": lambda: _fmt_qty(r)}.get(f, lambda: str(r.extras.get(f) or ""))()


def _same(f: str, a: SourceRow, b: SourceRow, s: Settings) -> bool | None:
    """Field equality; None means 'not comparable' (missing value or incomparable unit)."""
    if f == "date":
        if not a.date or not b.date:
            return None
        return abs((a.date - b.date).days) <= s.tolerances.date_days
    if f == "supplier":
        if not a.supplier_norm or not b.supplier_norm:
            return None
        return fuzz.token_set_ratio(a.supplier_norm, b.supplier_norm) >= \
            s.matching.supplier_threshold
    if f == "item":
        if not a.item_norm or not b.item_norm:
            return None
        fa, fb = ocr_fold(a.item_norm, s), ocr_fold(b.item_norm, s)
        return fa == fb or fuzz.token_set_ratio(fa, fb) >= s.matching.item_threshold
    if f == "lot":
        return None if not a.lot or not b.lot else a.lot == b.lot
    if f == "qty":
        if a.qty_base is None or b.qty_base is None:
            return None
        if a.base_unit and b.base_unit and a.base_unit != b.base_unit:
            return None
        return qty_equal(a.qty_base, b.qty_base, s)
    va, vb = a.extras.get(f), b.extras.get(f)
    if va in (None, "") or vb in (None, ""):
        return None
    return str(va).strip().casefold() == str(vb).strip().casefold()


def compare_group(g: Group, s: Settings) -> None:
    """Fill g.mismatches and g.hints by comparing each field across source primaries."""
    prims = {src: g.primary(src) for src in g.sources}
    if len(prims) < 2:
        return
    truth = s.matching.source_of_truth
    for f in s.matching.compare_fields:
        srcs = list(prims)
        comparable = [(x, y) for i, x in enumerate(srcs) for y in srcs[i + 1:]
                      if _same(f, prims[x], prims[y], s) is not None]
        if not comparable or all(_same(f, prims[x], prims[y], s) for x, y in comparable):
            continue
        values = {src: _display(prims[src], f) for src in srcs}
        clusters: list[list[str]] = []
        for src in srcs:
            for c in clusters:
                if _same(f, prims[src], prims[c[0]], s):
                    c.append(src)
                    break
            else:
                clusters.append([src])
        clusters.sort(key=len, reverse=True)
        if truth in prims:
            agree = next(c for c in clusters if truth in c)
            outliers = [x for x in srcs if x not in agree]
            hint = f"source of truth {truth} says {values[truth]}"
        elif len(clusters[0]) >= 2 and len(clusters[0]) > len(clusters[1]):
            outliers = [x for x in srcs if x not in clusters[0]]
            hint = (f"{len(clusters[0])} of {len(srcs)} agree on {values[clusters[0][0]]}; "
                    f"outlier: {', '.join(outliers)}")
        else:
            outliers, hint = [], "no majority"
        g.mismatches.append(FieldMismatch(f, values, hint, outliers))
        _field_hints(g, f, prims, s)
    _unit_checks(g, prims, s)


def _field_hints(g: Group, f: str, prims: dict[str, SourceRow], s: Settings) -> None:
    srcs = list(prims)
    for i, x in enumerate(srcs):
        for y in srcs[i + 1:]:
            a, b = prims[x], prims[y]
            if f == "date" and a.date and b.date and abs((a.date - b.date).days) == 1:
                g.hints.append(f"DATE_OFF_BY_ONE: {x} {a.date} vs {y} {b.date}")
            if f == "lot" and is_transposition(a.lot, b.lot):
                g.hints.append(f"TRANSPOSITION_HINT: lot {x} {a.lot} vs {y} {b.lot}")


def _unit_checks(g: Group, prims: dict[str, SourceRow], s: Settings) -> None:
    """Flag incomparable units; hint when a unit looks mislabelled."""
    srcs = list(prims)
    for i, x in enumerate(srcs):
        for y in srcs[i + 1:]:
            a, b = prims[x], prims[y]
            if a.qty is None or b.qty is None or not a.base_unit or not b.base_unit:
                continue
            if a.base_unit != b.base_unit:
                for r, o in ((a, y), (b, x)):
                    if not any(fl.type == FlagType.UNIT_INCOMPARABLE for fl in r.flags):
                        r.flags.append(Flag(
                            type=FlagType.UNIT_INCOMPARABLE, field="qty",
                            message=f"{r.unit} vs {o}'s unit cannot be converted; enter the "
                                    "conversion or mark as exception"))
            elif a.unit != b.unit and a.qty == b.qty:
                third = [prims[z] for z in srcs if z not in (x, y)]
                culprit = x if any(t.unit == b.unit for t in third) else y
                g.hints.append(f"UNIT_MISLABEL_SUSPECTED: {x} and {y} both say {a.qty} but "
                               f"units differ ({a.unit} vs {b.unit}); check {culprit}")


# ---------- suspect entries ----------

def flag_suspect_entries(groups: list[Group], rows: list[SourceRow], s: Settings) -> None:
    """Late/backdated entries: hint when matched elsewhere, review item when single-source."""
    by_src: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.extras.get("entered_by"):
            by_src[r.source][str(r.extras["entered_by"]).strip().casefold()] += 1
    for g in groups:
        for src in g.sources:
            r = g.primary(src)
            reasons = []
            if r.entered_at and r.date:
                lag = (r.entered_at.date() - r.date).days
                if lag > s.thresholds.late_entry_days:
                    reasons.append(f"entered {lag} days after the document date")
            who = str(r.extras.get("entered_by") or "").strip().casefold()
            common = by_src[src].most_common(1)
            if who and common and common[0][0] != who and by_src[src][who] <= max(
                    1, 0.1 * sum(by_src[src].values())):
                reasons.append(f"entered by '{r.extras['entered_by']}', not the usual user")
            if not reasons:
                continue
            text = "; ".join(reasons)
            if len(g.sources) == 1 and r.entered_at and r.date and (
                    r.entered_at.date() - r.date).days > s.thresholds.late_entry_days:
                r.flags.append(Flag(type=FlagType.SUSPECT_ENTRY,
                                    message=f"only in {src}, {text}. Legitimate?"))
            g.hints.append(f"LATE_OR_BACKDATED: {src} {text}")


def date_gap_days(a: dt.date | None, b: dt.date | None) -> int | None:
    """Absolute day difference, or None."""
    return abs((a - b).days) if a and b else None
