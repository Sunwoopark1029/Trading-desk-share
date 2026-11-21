"""SEC companyfacts -> annual / quarterly / TTM statement tables (phase 6 part B). Pure functions, unit-tested.

Rules (from what SEC's files actually contain, checked on AAPL / NVDA / TSM 2026-11-21):
- Only facts from annual reports (10-K, 20-F, 40-F, amendments, 10-KT) and quarterly reports (10-Q) are used.
- Periods are recognized by length, not by the `fp` field (10-Ks also carry 3-month facts tagged fp=FY):
  annual 365 +/- 30 days, quarter 91 +/- 30 days, half year and nine months for year-to-date cash-flow facts.
- One value per period: the LATEST filing wins (restated numbers replace the originals).
- `fy` is the fiscal year of the filing, so prior-year comparatives carry a later fy. A fiscal year is labelled by
  the smallest fy among annual-report facts for that exact period, else the calendar year of its end date, and is
  shown with its end date ("FY2026, ended 2026-01-25").
- Quarterly cash-flow facts are year-to-date: Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M. Any value obtained by
  subtraction is marked derived. Only money flows are derived; EPS and share counts are never subtracted.
- Each line uses the candidate concepts present in the file, ranked by their latest period; each period is filled
  from the first candidate that reports it. A line with nothing reported is None ("not reported"), never 0.
- TTM = the four latest consecutive quarters (money flows only); with no quarterly filings (foreign 20-F filers)
  the latest fiscal year is used and labelled as such. Balance items in the TTM column are the latest value.
- A share-count jump between fiscal years is flagged (possible split); values are never adjusted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.fundamentals.concepts import LINES, Line

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTER_FORMS = {"10-Q", "10-Q/A"}
ANNUAL_DAYS = (335, 395)
QUARTER_DAYS = (61, 121)
YTD_DAYS = {2: (152, 212), 3: (243, 303)}        # half year, nine months
EDGE = timedelta(days=4)                          # start / end dates may differ by a few days between filings
SPLIT_RATIO = Decimal("1.5")


@dataclass(frozen=True)
class Fact:
    concept: str            # "us-gaap:Revenues"
    start: date | None
    end: date
    val: Decimal
    accn: str
    form: str
    fp: str | None
    fy: int | None
    filed: date

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


@dataclass(frozen=True)
class Period:
    key: str                # "FY2025", "FY2026Q2", "TTM"
    kind: Literal["annual", "quarter", "ttm"]
    fiscal_year: int
    quarter: int | None
    start: date | None
    end: date
    label: str


@dataclass(frozen=True)
class Cell:
    value: Decimal | None
    concept: str | None = None
    accn: str | None = None
    form: str | None = None
    filed: date | None = None
    derived: bool = False
    note: str | None = None     # "not reported", "derived: FY - 9M", ...


@dataclass
class Statements:
    currency: str
    annual: list[Period]                      # newest first
    quarterly: list[Period]                   # newest first
    ttm: Period | None
    rows: dict[str, dict[str, Cell]]          # line id -> period key -> cell
    warnings: list[str] = field(default_factory=list)
    latest_filed: date | None = None
    entity: str | None = None
    taxonomy: str | None = None               # "us-gaap" / "ifrs-full"


# ── reading facts ────────────────────────────────────────────────────────

def _d(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def read_facts(data: dict[str, Any], taxonomy: str, concept: str, unit: str) -> list[Fact]:
    rows = (((data.get("facts") or {}).get(taxonomy) or {}).get(concept) or {}).get("units", {}).get(unit) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        form = r.get("form")
        if form not in ANNUAL_FORMS and form not in QUARTER_FORMS:
            continue
        end, filed, val = _d(r.get("end")), _d(r.get("filed")), _num(r.get("val"))
        if end is None or filed is None or val is None:
            continue
        start = _d(r.get("start")) if "start" in r else None
        fy = r.get("fy") if isinstance(r.get("fy"), int) and not isinstance(r.get("fy"), bool) else None
        out.append(Fact(f"{taxonomy}:{concept}", start, end, val, str(r.get("accn") or ""), form,
                        r.get("fp") if isinstance(r.get("fp"), str) else None, fy, filed))
    return out


def reporting_currency(data: dict[str, Any]) -> str:
    """The money unit used most by the income-statement concepts in the NEWEST annual report (USD for US filers,
    TWD for TSM ...). Only the newest report counts, so a company that changed currency keeps its recent years."""
    by_filed: dict[str, Counter[str]] = {}
    for line in LINES:
        if line.statement != "income" or line.measure != "money":
            continue
        for tax, concept in line.candidates:
            units = ((data.get("facts") or {}).get(tax) or {}).get(concept, {}).get("units", {})
            for unit, rows in units.items():
                if not (len(unit) == 3 and unit.isalpha() and unit.isupper()):
                    continue
                for r in rows:
                    if isinstance(r, dict) and r.get("form") in ANNUAL_FORMS and isinstance(r.get("filed"), str):
                        by_filed.setdefault(r["filed"], Counter())[unit] += 1
    if not by_filed:
        return "USD"
    return by_filed[max(by_filed)].most_common(1)[0][0]


def _unit(line: Line, currency: str) -> str:
    return currency if line.measure == "money" else f"{currency}/shares" if line.measure == "per_share" else "shares"


def _latest(facts: list[Fact]) -> Fact | None:
    return max(facts, key=lambda f: (f.filed, f.accn)) if facts else None


def _near(a: date | None, b: date | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= EDGE


def _len_ok(f: Fact, rng: tuple[int, int]) -> bool:
    return f.days is not None and rng[0] <= f.days <= rng[1]


# ── fiscal calendar ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class FiscalYear:
    label: int
    start: date
    end: date
    complete: bool          # an annual report covers it (False = the year in progress)


def fiscal_years(all_facts: list[Fact]) -> list[FiscalYear]:
    """Fiscal years from annual-length facts in annual reports, oldest first, plus the year in progress."""
    periods: dict[tuple[date, date], list[Fact]] = {}
    for f in all_facts:
        if f.form in ANNUAL_FORMS and f.start is not None and _len_ok(f, ANNUAL_DAYS):
            periods.setdefault((f.start, f.end), []).append(f)
    chosen: list[tuple[date, date, list[Fact]]] = []
    for (start, end), facts in sorted(periods.items(), key=lambda kv: kv[0][1]):
        if chosen and abs(chosen[-1][1] - end) <= timedelta(days=20):     # same year seen with a slightly
            if len(facts) > len(chosen[-1][2]):                            # different start/end: keep the
                chosen[-1] = (start, end, facts)                           # better-supported one
            continue
        chosen.append((start, end, facts))
    # A period's fy is read only from the report whose OWN latest annual period it is (not from comparatives in
    # later reports), then labels are forced strictly increasing so two years can never share a label.
    own_end: dict[str, date] = {}
    for f in all_facts:
        if f.form in ANNUAL_FORMS and f.start is not None and _len_ok(f, ANNUAL_DAYS):
            own_end[f.accn] = max(own_end.get(f.accn, f.end), f.end)
    years: list[FiscalYear] = []
    for start, end, facts in chosen:
        fys = [f.fy for f in facts if f.fy is not None and abs(own_end.get(f.accn, end) - end) <= timedelta(days=20)]
        label = min(fys) if fys else end.year
        if years and label <= years[-1].label:
            label = years[-1].label + 1
        years.append(FiscalYear(label, start, end, True))
    if years:                                             # the fiscal year in progress (quarters only so far)
        last = years[-1]
        # start after the latest period ANY annual report covers (a short 10-KT transition period counts too)
        anchor = max([last.end] + [f.end for f in all_facts if f.form in ANNUAL_FORMS and f.start is not None])
        later_q = [f for f in all_facts if f.form in QUARTER_FORMS and f.end > anchor + EDGE]
        if later_q:
            nxt_start = anchor + timedelta(days=1)
            label = max((f.fy for f in later_q if f.fy is not None), default=last.label + 1)
            years.append(FiscalYear(max(label, last.label + 1), nxt_start, nxt_start + timedelta(days=364), False))
    return years


def _quarter_no(fy: FiscalYear, end: date) -> int:
    return max(1, min(4, round((end - fy.start).days / 91.31)))


def quarter_ends(fy: FiscalYear, all_facts: list[Fact]) -> dict[int, date]:
    """Quarter number -> quarter end date inside one fiscal year, from quarter-length and year-to-date facts."""
    votes: dict[int, Counter[date]] = {}
    for f in all_facts:
        if f.start is None or not (fy.start - EDGE <= f.end <= fy.end + EDGE):
            continue
        quarterly = _len_ok(f, QUARTER_DAYS) and f.start >= fy.start - EDGE
        ytd = _near(f.start, fy.start) and any(_len_ok(f, r) for r in YTD_DAYS.values())
        if quarterly or ytd:
            votes.setdefault(_quarter_no(fy, f.end), Counter())[f.end] += 1
    ends = {q: c.most_common(1)[0][0] for q, c in votes.items()}
    if fy.complete:
        ends[4] = fy.end
    return dict(sorted(ends.items()))


# ── values ───────────────────────────────────────────────────────────────

def _cell(f: Fact, derived: bool = False, note: str | None = None) -> Cell:
    return Cell(f.val, f.concept, f.accn, f.form, f.filed, derived, note)


def annual_value(facts: list[Fact], line: Line, fy: FiscalYear) -> Cell | None:
    if line.instant:
        hit = _latest([f for f in facts if f.start is None and f.end == fy.end])
    else:
        hit = _latest([f for f in facts if f.end == fy.end and _len_ok(f, ANNUAL_DAYS) and _near(f.start, fy.start)])
    return _cell(hit) if hit else None


def quarter_value(facts: list[Fact], line: Line, fy: FiscalYear, q: int, ends: dict[int, date]) -> Cell | None:
    end = ends.get(q)
    if end is None:
        return None
    if line.instant:
        hit = _latest([f for f in facts if f.start is None and f.end == end])
        return _cell(hit) if hit else None
    prev_end = ends.get(q - 1) if q > 1 else fy.start - timedelta(days=1)
    discrete = _latest([f for f in facts if f.end == end and _len_ok(f, QUARTER_DAYS)
                        and (prev_end is None or _near(f.start, prev_end + timedelta(days=1)))])
    if discrete:
        return _cell(discrete)
    if not line.derivable or prev_end is None:
        return None

    def ytd_all(k: int) -> list[Fact]:
        """Every year-to-date fact from the fiscal-year start to quarter k's end (k=4: the annual facts)."""
        e = ends.get(k)
        if e is None:
            return []
        rng = QUARTER_DAYS if k == 1 else ANNUAL_DAYS if k == 4 else YTD_DAYS[k]
        return [f for f in facts if f.end == e and _len_ok(f, rng) and _near(f.start, fy.start)]

    def reported_ytd(k: int) -> Fact | None:
        return _latest(ytd_all(k))

    def same_vintage(k: int, since: date) -> Fact | None:
        """Quarter k's YTD fact as first reported on/after `since` (the vintage of the earlier operand), so a later
        restatement of the bigger period is never subtracted from an unrestated smaller one."""
        later = [f for f in ytd_all(k) if f.filed >= since]
        return min(later, key=lambda f: (f.filed, f.accn)) if later else reported_ytd(k)

    def discrete(k: int) -> Fact | None:
        e, pe = ends.get(k), (ends.get(k - 1) if k > 1 else fy.start - timedelta(days=1))
        if e is None or pe is None:
            return None
        return _latest([f for f in facts if f.end == e and _len_ok(f, QUARTER_DAYS)
                        and _near(f.start, pe + timedelta(days=1))])

    if reported_ytd(q) is None:
        return None
    names = {1: "Q1", 2: "H1", 3: "9M", 4: "FY"}

    def vintage_note(cur: Fact) -> str:
        return "" if cur is reported_ytd(q) or cur.val == reported_ytd(q).val else \
            f" (as first reported; {names[q]} later restated)"  # type: ignore[union-attr]

    prev = reported_ytd(q - 1)                       # route 1: YTD(q) - YTD(q-1), e.g. Q3 = 9M - H1
    if prev is not None:
        cur = same_vintage(q, prev.filed)
        assert cur is not None
        return Cell(cur.val - prev.val, cur.concept, cur.accn, cur.form, cur.filed, True,
                    f"derived: {names[q]} - {names[q - 1]}{vintage_note(cur)}")
    parts = [discrete(k) for k in range(1, q)]       # route 2: YTD(q) - reported quarters, e.g. Q4 = FY - (Q1+Q2+Q3)
    if q > 1 and all(p is not None for p in parts):
        total = sum((p.val for p in parts if p), Decimal(0))
        cur = same_vintage(q, max(p.filed for p in parts if p))
        assert cur is not None
        return Cell(cur.val - total, cur.concept, cur.accn, cur.form, cur.filed, True,
                    f"derived: {names[q]} - ({'+'.join(f'Q{k}' for k in range(1, q))}){vintage_note(cur)}")
    return None


# ── building the tables ──────────────────────────────────────────────────

def _ranked_candidates(data: dict[str, Any], line: Line, currency: str) -> list[list[Fact]]:
    """Fact lists per candidate concept present in the file, most recently reported first."""
    present = []
    for i, (tax, concept) in enumerate(line.candidates):
        facts = read_facts(data, tax, concept, _unit(line, currency))
        if facts:
            present.append((max(f.end for f in facts), -i, facts))
    return [facts for _, _, facts in sorted(present, key=lambda t: (t[0], t[1]), reverse=True)]


def _first(cands: list[list[Fact]], get: Any) -> Cell:
    for facts in cands:
        c = get(facts)
        if c is not None:
            return c
    return Cell(None, note="not reported")


def build(data: dict[str, Any], years: int = 5, quarters: int = 8) -> Statements:
    currency = reporting_currency(data)
    cands = {line.id: _ranked_candidates(data, line, currency) for line in LINES}
    every = [f for lists in cands.values() for facts in lists for f in facts]
    fys = fiscal_years([f for line in LINES if not line.instant for facts in cands[line.id] for f in facts])
    complete = [fy for fy in fys if fy.complete]
    annual_periods: list[tuple[Period, FiscalYear]] = []
    for fy in reversed(complete[-years:] if years else []):
        annual_periods.append((Period(f"FY{fy.label}", "annual", fy.label, None, fy.start, fy.end,
                                      f"FY{fy.label}, ended {fy.end.isoformat()}"), fy))
    quarter_periods: list[tuple[Period, FiscalYear, int, dict[int, date]]] = []
    for fy in reversed(fys):
        ends = quarter_ends(fy, every)
        for q in sorted(ends, reverse=True):
            if not fy.complete or q < 4 or any(_len_ok(f, QUARTER_DAYS) or (f.form in QUARTER_FORMS)
                                                for f in every if fy.start - EDGE <= f.end <= fy.end + EDGE):
                quarter_periods.append((Period(f"FY{fy.label}Q{q}", "quarter", fy.label, q,
                                               (ends.get(q - 1) or fy.start - timedelta(days=1)) + timedelta(days=1)
                                               if q > 1 else fy.start, ends[q],
                                               f"Q{q} FY{fy.label}, ended {ends[q].isoformat()}"), fy, q, ends))
    has_quarters = any(f.form in QUARTER_FORMS for f in every)
    if not has_quarters:
        quarter_periods = []
    quarter_periods = quarter_periods[:quarters] if quarters else []

    rows: dict[str, dict[str, Cell]] = {}
    for line in LINES:
        cs = cands[line.id]
        row: dict[str, Cell] = {}
        for p, fy in annual_periods:
            row[p.key] = _first(cs, lambda facts, fy=fy: annual_value(facts, line, fy))
        for p, fy, q, ends in quarter_periods:
            row[p.key] = _first(cs, lambda facts, fy=fy, q=q, ends=ends: quarter_value(facts, line, fy, q, ends))
        rows[line.id] = row

    ttm, warnings = _ttm(rows, annual_periods, quarter_periods, has_quarters)
    st = Statements(currency, [p for p, _ in annual_periods], [p for p, *_ in quarter_periods], ttm, rows, warnings,
                    max((f.filed for f in every), default=None), data.get("entityName"),
                    "ifrs-full" if "ifrs-full" in (data.get("facts") or {}) and "us-gaap" not in (data.get("facts") or {})
                    else "us-gaap")
    if currency != "USD":
        st.warnings.append(f"Reported in {currency}, not USD")
    if not has_quarters:
        st.warnings.append("No quarterly reports (10-Q) in SEC's data: annual figures only")
    st.warnings.extend(split_flags(rows, [p for p, _ in annual_periods]))
    return st


def _ttm(rows: dict[str, dict[str, Cell]], annual: list[tuple[Period, FiscalYear]],
         quarters: list[tuple[Period, FiscalYear, int, dict[int, date]]], has_quarters: bool
         ) -> tuple[Period | None, list[str]]:
    warnings: list[str] = []
    if has_quarters and annual and quarters and quarters[0][0].end < annual[0][0].end - EDGE:
        has_quarters = False         # quarterly reports stopped (e.g. moved to 20-F): old quarters make no TTM
        warnings.append(f"Quarterly reports stop at {quarters[0][0].end.isoformat()}: TTM shows the latest fiscal year")
    if not has_quarters:
        if not annual:
            return None, warnings
        p = annual[0][0]
        ttm = Period("TTM", "ttm", p.fiscal_year, None, p.start, p.end, f"Latest fiscal year (FY{p.fiscal_year}), "
                     "no quarterly reports")
        for line in LINES:
            c = rows[line.id].get(p.key, Cell(None, note="not reported"))
            rows[line.id]["TTM"] = Cell(c.value, c.concept, c.accn, c.form, c.filed, c.derived,
                                        "latest fiscal year (no quarterly reports)" if c.value is not None else c.note)
        return ttm, warnings
    if len(quarters) < 4:
        return None, warnings
    last4 = quarters[:4]
    consecutive = all(_next_q(last4[i + 1][0]) == (last4[i][0].fiscal_year, last4[i][0].quarter) for i in range(3))
    p0 = last4[0][0]
    ttm = Period("TTM", "ttm", p0.fiscal_year, p0.quarter, last4[-1][0].start, p0.end,
                 f"TTM to {p0.end.isoformat()}")
    for line in LINES:
        cells = [rows[line.id].get(p.key) for p, *_ in last4]
        if line.instant:
            latest = next((c for c in cells if c and c.value is not None), None)
            rows[line.id]["TTM"] = latest or Cell(None, note="not reported")
        elif line.measure != "money":
            rows[line.id]["TTM"] = Cell(None, note="not additive: no TTM per-share value (P/E uses market cap / TTM net income)")
        elif not consecutive or any(c is None or c.value is None for c in cells):
            rows[line.id]["TTM"] = Cell(None, note="missing quarter: no TTM")
        else:
            rows[line.id]["TTM"] = Cell(sum((c.value for c in cells if c), Decimal(0)),
                                        cells[0].concept if cells[0] else None,
                                        derived=True, note="sum of the last 4 quarters")
    if not consecutive:
        warnings.append("The last four quarters are not consecutive: no TTM")
    return ttm, warnings


def _next_q(p: Period) -> tuple[int, int]:
    assert p.quarter is not None
    return (p.fiscal_year + 1, 1) if p.quarter == 4 else (p.fiscal_year, p.quarter + 1)


def split_flags(rows: dict[str, dict[str, Cell]], annual: list[Period]) -> list[str]:
    """Share-count jumps between consecutive fiscal years (possible split). Values are never adjusted."""
    out = []
    shares = rows.get("shares_diluted", {})
    for newer, older in zip(annual, annual[1:]):
        a, b = shares.get(newer.key), shares.get(older.key)
        if a and b and a.value and b.value and b.value > 0:
            ratio = a.value / b.value
            if ratio >= SPLIT_RATIO or ratio <= 1 / SPLIT_RATIO:
                out.append(f"Diluted shares jump {ratio:.2f}x between FY{older.fiscal_year} and FY{newer.fiscal_year} "
                           "as reported: likely a stock split restated in later filings only (the split itself may "
                           "be later); per-share values before and after are on different bases (not adjusted)")
    return out
