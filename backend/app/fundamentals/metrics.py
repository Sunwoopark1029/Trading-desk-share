"""Growth, margins, balance metrics and valuation at the live price (phase 6 part B). Pure functions, unit-tested.

Every number here is calculated in code from statements.py tables, the live Toss price and Toss shares outstanding.
A metric that cannot be calculated honestly is None with a reason ("n/a (loss)", "zero revenue", "split in window",
"not enough history"), never 0.
- Market cap = Toss last price x Toss sharesOutstanding; for depositary receipts it is labelled "est." (open question
  3: Toss seems to report the ADS count). EV = market cap + long-term debt - cash (current debt is not included).
- P/E = market cap / TTM net income (so share-count changes and splits do not distort it); P/S, P/B, EV/EBIT,
  FCF yield likewise from market cap / EV and TTM figures. FCF = operating cash flow - capital expenditure.
- Valuation needs statements in the price currency (USD). A company reporting in another currency (TSMC: TWD) gets
  no multiples: no verified source for that exchange rate exists yet, and its ADR ratio is unknown.
- CAGR = (end / start) ^ (1 / years) - 1, only when start and end are both > 0.
- EPS-based growth is refused across a flagged share-count jump (possible split, never adjusted).
- Own-history P/E at each fiscal-year end = (price then x shares outstanding then) / that year's net income; median of
  at least 3 years. Implied price = median P/E x TTM net income / Toss shares outstanding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from statistics import median
from typing import Any

from app.fundamentals.statements import SPLIT_RATIO, Cell, Statements

D0 = Decimal(0)
MIN_HISTORY = 3


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    value: Decimal | None
    kind: str                      # "money" | "pct" (growth, signed) | "level" (margin / ROE / yield) | "multiple" ...
    period: str                    # "TTM", "FY2025", "FY2022->FY2025", "live"
    note: str | None = None        # reason when value is None, or "est." etc.
    currency: str | None = None    # for money / per-share values
    basis: tuple[str, ...] = ()    # ids / inputs it was calculated from


@dataclass
class Valuation:
    price: Decimal | None
    price_currency: str
    price_time: str | None
    shares: Decimal | None         # Toss sharesOutstanding
    security_type: str | None
    metrics: list[Metric] = field(default_factory=list)


def _v(st: Statements, line: str, key: str) -> Decimal | None:
    c: Cell | None = st.rows.get(line, {}).get(key)
    return c.value if c is not None else None


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def cagr(start: Decimal | None, end: Decimal | None, years: int) -> tuple[Decimal | None, str | None]:
    """Compound annual growth as a fraction (0.10 = 10 %), or (None, reason)."""
    if start is None or end is None:
        return None, "not reported"
    if years <= 0:
        return None, "no period"
    if start <= 0 or end <= 0:
        return None, "n/a (zero or negative start / end)"
    with localcontext() as ctx:
        ctx.prec = 34
        return ((end / start).ln() / Decimal(years)).exp() - 1, None


def yoy(prev: Decimal | None, cur: Decimal | None) -> tuple[Decimal | None, str | None]:
    if prev is None or cur is None:
        return None, "not reported"
    if prev <= 0:
        return None, "n/a (zero or negative base)"
    return cur / prev - 1, None


def _split_between(st: Statements, older_fy: int, newer_fy: int) -> bool:
    """A flagged share-count jump INSIDE this window: its older side is at or after older_fy and before newer_fy.
    A jump that ends at older_fy (both endpoints already on the new basis) does not count."""
    for w in st.warnings:
        if "stock split" not in w:
            continue
        for fy in range(older_fy, newer_fy):
            if f"between FY{fy} and" in w:
                return True
    return False


def _ttm_tag(st: Statements) -> tuple[str, str | None]:
    """Display tag for the TTM column: "TTM", or "FY2024" when TTM fell back to the latest fiscal year."""
    t = st.ttm
    if t is None or t.quarter is not None:
        return "TTM", None
    return f"FY{t.fiscal_year}", "latest fiscal year (no recent quarterly reports)"


def _note(*parts: str | None) -> str | None:
    kept = [p for p in parts if p]
    return "; ".join(kept) if kept else None


def growth(st: Statements) -> list[Metric]:
    out: list[Metric] = []
    fy = st.annual                                   # newest first
    for line, label, per_share in (("revenue", "Revenue", False), ("net_income", "Net income", False),
                                   ("eps_diluted", "EPS (diluted)", True)):
        short = {"revenue": "rev", "net_income": "ni", "eps_diluted": "eps"}[line]
        if len(fy) >= 2:
            a, b = fy[1], fy[0]
            if per_share and _split_between(st, a.fiscal_year, b.fiscal_year):
                val, note = None, "n/a (possible split in window)"
            else:
                val, note = yoy(_v(st, line, a.key), _v(st, line, b.key))
            out.append(Metric(f"{short}_yoy", f"{label} growth (YoY)", val, "pct", f"{a.key}->{b.key}", note,
                              basis=(f"{a.key.lower()}.{line}", f"{b.key.lower()}.{line}")))
        for n in (3, 5):
            if len(fy) > n:
                a, b = fy[n], fy[0]
                if per_share and _split_between(st, a.fiscal_year, b.fiscal_year):
                    val, note = None, "n/a (possible split in window)"
                else:
                    val, note = cagr(_v(st, line, a.key), _v(st, line, b.key), n)
                out.append(Metric(f"{short}_cagr_{n}y", f"{label} CAGR ({n}y)", val, "pct", f"{a.key}->{b.key}", note,
                                  basis=(f"{a.key.lower()}.{line}", f"{b.key.lower()}.{line}")))
            else:
                out.append(Metric(f"{short}_cagr_{n}y", f"{label} CAGR ({n}y)", None, "pct", f"{n}y",
                                  "not enough history"))
    return out


def fcf(st: Statements, key: str) -> Decimal | None:
    ocf, capex = _v(st, "operating_cash_flow", key), _v(st, "capex", key)
    if ocf is None or capex is None:
        return None
    return ocf - abs(capex)                         # capex is reported as a payment (positive number)


def margins(st: Statements) -> list[Metric]:
    out: list[Metric] = []
    ttm_tag, ttm_note = _ttm_tag(st)
    periods = [("TTM", "ttm", ttm_tag, ttm_note)] + \
        ([(st.annual[0].key, st.annual[0].key.lower(), st.annual[0].key, None)] if st.annual else [])
    for key, tag, shown, extra in periods:
        if key == "TTM" and st.ttm is None:
            continue
        rev = _v(st, "revenue", key)
        for line, label in (("gross_profit", "Gross margin"), ("operating_income", "Operating margin"),
                            ("net_income", "Net margin"), ("fcf", "FCF margin")):
            num = fcf(st, key) if line == "fcf" else _v(st, line, key)
            if rev is not None and rev == 0:
                val, note = None, "n/a (zero revenue)"
            elif num is None or rev is None:
                val, note = None, "not reported"
            else:
                val, note = num / rev, None
            short = {"gross_profit": "gross", "operating_income": "op", "net_income": "net", "fcf": "fcf"}[line]
            out.append(Metric(f"{short}_margin_{tag}", f"{label} ({shown})", val, "level", shown, _note(note, extra),
                              basis=(f"{tag}.{line}", f"{tag}.revenue")))
    return out


def balance(st: Statements) -> list[Metric]:
    out: list[Metric] = []
    key = "TTM" if st.ttm is not None else (st.annual[0].key if st.annual else None)
    if key is None:
        return out
    shown, extra = _ttm_tag(st) if key == "TTM" else (key, None)
    cash, debt, eq, ni = (_v(st, "cash", key), _v(st, "long_term_debt", key), _v(st, "equity", key),
                          _v(st, "net_income", key))
    net_debt = None if cash is None or debt is None else debt - cash
    out.append(Metric("net_debt", "Net debt (long-term debt − cash)", net_debt, "money", shown,
                      "not reported" if net_debt is None else _note("long-term debt only; current debt not included"),
                      st.currency, ("long_term_debt", "cash")))
    if eq is not None and eq <= 0:
        out.append(Metric("roe", "Return on equity", None, "level", shown, "n/a (zero or negative equity)"))
    else:
        r = _div(ni, eq)
        out.append(Metric("roe", f"Return on equity ({shown})", r, "level", shown,
                          _note("not reported" if r is None else None, extra), basis=("net_income", "equity")))
    f = fcf(st, key)
    out.append(Metric("fcf_ttm", f"Free cash flow ({shown})", f, "money", shown,
                      _note("not reported" if f is None else None, extra), st.currency,
                      ("operating_cash_flow", "capex")))
    if len(st.annual) > 3:
        a, b = st.annual[3], st.annual[0]
        if _split_between(st, a.fiscal_year, b.fiscal_year):
            val, note = None, "n/a (possible split in window)"
        else:
            val, note = yoy(_v(st, "shares_diluted", a.key), _v(st, "shares_diluted", b.key))
        out.append(Metric("shares_change_3y", "Diluted share count change (3y)", val, "pct", f"{a.key}->{b.key}", note))
    return out


def valuation(st: Statements, price: Decimal | None, shares: Decimal | None, *, price_currency: str = "USD",
              price_time: str | None = None, security_type: str | None = None) -> Valuation:
    v = Valuation(price, price_currency, price_time, shares, security_type)
    est = "est. (depositary receipt: Toss share count)" if security_type == "DEPOSITARY_RECEIPT" else None
    shown, extra = _ttm_tag(st)
    if st.currency != price_currency:
        reason = f"n/a (statements in {st.currency}, price in {price_currency}: no verified exchange rate)"
        for mid, label, kind in (("mcap", "Market cap", "money"), ("ev", "Enterprise value", "money"),
                                 ("pe_ttm", f"P/E ({shown})", "multiple"), ("ps_ttm", f"P/S ({shown})", "multiple"),
                                 ("pb", "P/B", "multiple"), ("ev_ebit_ttm", f"EV/EBIT ({shown})", "multiple"),
                                 ("fcf_yield_ttm", f"FCF yield ({shown})", "level")):
            v.metrics.append(Metric(mid, label, None, kind, "live", reason))
        return v
    key = "TTM" if st.ttm is not None else (st.annual[0].key if st.annual else "TTM")
    mcap = price * shares if price is not None and shares is not None and shares > 0 else None
    cash, debt = _v(st, "cash", key), _v(st, "long_term_debt", key)
    ev = mcap + debt - cash if mcap is not None and cash is not None and debt is not None else None
    ni, rev, eq, ebit = (_v(st, "net_income", key), _v(st, "revenue", key), _v(st, "equity", key),
                         _v(st, "operating_income", key))
    f = fcf(st, key)

    def mult(mid: str, label: str, num: Decimal | None, den: Decimal | None, loss_note: str,
             basis: tuple[str, ...]) -> Metric:
        if mcap is None:
            return Metric(mid, label, None, "multiple", shown, "no live price / shares")
        if num is None or den is None:
            return Metric(mid, label, None, "multiple", shown, "not reported")
        if num <= 0:
            return Metric(mid, label, None, "multiple", shown, "n/a (zero or negative EV)")
        if den <= 0:
            return Metric(mid, label, None, "multiple", shown, loss_note)
        return Metric(mid, label, num / den, "multiple", shown, _note(est, extra), basis=basis)

    v.metrics += [
        Metric("mcap", "Market cap", mcap, "money", "live", est if mcap is not None else "no live price / shares",
               price_currency, ("price", "toss.shares_outstanding")),
        Metric("ev", "Enterprise value", ev, "money", "live",
               _note(est, "long-term debt only") if ev is not None
               else ("no live price / shares" if mcap is None else "not reported"),
               price_currency, ("mcap", "long_term_debt", "cash")),
        mult("pe_ttm", f"P/E ({shown})", mcap, ni, "n/a (loss)", ("mcap", "ttm.net_income")),
        mult("ps_ttm", f"P/S ({shown})", mcap, rev, "n/a (zero revenue)", ("mcap", "ttm.revenue")),
        mult("pb", "P/B", mcap, eq, "n/a (zero or negative equity)", ("mcap", "equity")),
        mult("ev_ebit_ttm", f"EV/EBIT ({shown})", ev, ebit, "n/a (operating loss)", ("ev", "ttm.operating_income")),
    ]
    if mcap is None or f is None:
        v.metrics.append(Metric("fcf_yield_ttm", f"FCF yield ({shown})", None, "level", shown,
                                "no live price / shares" if mcap is None else "not reported"))
    else:
        v.metrics.append(Metric("fcf_yield_ttm", f"FCF yield ({shown})", f / mcap, "level", shown, _note(est, extra),
                                basis=("fcf_ttm", "mcap")))
    return v


def own_history_pe(st: Statements, year_end_prices: dict[date, Decimal]) -> tuple[list[tuple[str, Decimal]], str | None]:
    """(FY key, P/E) at each fiscal-year end with a price, shares outstanding then and a profit that year.

    Only years on the same share basis as the newest one: walking back, it stops at the first share-count jump of
    SPLIT_RATIO or more (pre-split shares would not match split-adjusted prices). Year-end prices must be on the same
    basis as the statements' latest shares (split-adjusted). Returns (values, note when a split cut the history)."""
    out: list[tuple[str, Decimal]] = []
    prev_sh: Decimal | None = None
    note = None
    for p in st.annual:                                # newest first
        sh = _v(st, "shares_outstanding", p.key)
        if sh is not None and sh > 0 and prev_sh is not None:
            r = prev_sh / sh
            if r >= SPLIT_RATIO or r <= 1 / SPLIT_RATIO:
                note = f"share-count jump before {p.key}: older years not used"
                break
        if sh is not None and sh > 0:
            prev_sh = sh
        price = year_end_prices.get(p.end)
        ni = _v(st, "net_income", p.key)
        if price is None or sh is None or ni is None or ni <= 0 or sh <= 0:
            continue
        out.append((p.key, price * sh / ni))
    return out, note


def implied(st: Statements, val: Valuation, own: list[tuple[str, Decimal]] | tuple[list[tuple[str, Decimal]], str | None],
            peer_pe_median: Decimal | None) -> list[Metric]:
    """Own-history / peer median P/E and the price each would imply (median P/E x TTM net income / Toss shares)."""
    own_list, cut_note = own if isinstance(own, tuple) else (own, None)
    out: list[Metric] = []
    key = "TTM" if st.ttm is not None else (st.annual[0].key if st.annual else "TTM")
    shown, extra = _ttm_tag(st)
    ni = _v(st, "net_income", key)
    same_cur = st.currency == val.price_currency
    dr = val.security_type == "DEPOSITARY_RECEIPT"
    cur_reason = f"n/a (statements in {st.currency}, price in {val.price_currency}: no verified exchange rate)"
    sec_sh = _v(st, "shares_outstanding", st.annual[0].key) if st.annual else None
    shares_differ = (val.shares is not None and sec_sh is not None and sec_sh > 0
                     and abs(val.shares / sec_sh - 1) > Decimal("0.05"))
    if not same_cur:
        own_med, own_none = None, cur_reason
    elif dr:
        own_med, own_none = None, "n/a (depositary receipt: ADS ratio unknown)"
    elif len(own_list) >= MIN_HISTORY:
        own_med, own_none = Decimal(median([pe for _, pe in own_list])), None
    else:
        own_med, own_none = None, _note(f"not enough history ({len(own_list)} of {MIN_HISTORY} fiscal years)", cut_note)
    for mid, label, med, note_none, period in (
            ("pe_own_median", f"P/E own-history median ({len(own_list)} FY ends)", own_med, own_none, "history"),
            ("pe_peer_median", "P/E peer median", peer_pe_median, "no peer P/E", "peers")):
        out.append(Metric(mid, label, med, "multiple", period,
                          note_none if med is None else (cut_note if mid == "pe_own_median" else None)))
        fv_id, fv_label = "fv_" + mid, f"Price at {label}"
        if med is None:
            out.append(Metric(fv_id, fv_label, None, "per_share", shown, note_none))
        elif not same_cur:
            out.append(Metric(fv_id, fv_label, None, "per_share", shown, cur_reason))
        elif val.shares is None or val.shares <= 0:
            out.append(Metric(fv_id, fv_label, None, "per_share", shown, "no live shares"))
        elif mid == "pe_own_median" and shares_differ:
            out.append(Metric(fv_id, fv_label, None, "per_share", shown, "n/a (Toss and SEC share counts differ)"))
        elif ni is None or ni <= 0:
            out.append(Metric(fv_id, fv_label, None, "per_share", shown, "n/a (loss)"))
        else:
            out.append(Metric(fv_id, fv_label, med * ni / val.shares, "per_share", shown,
                              _note("est. (depositary receipt)" if dr else None, extra),
                              val.price_currency, (mid, f"{key.lower()}.net_income", "toss.shares_outstanding")))
    return out


def all_metrics(st: Statements, price: Decimal | None, shares: Decimal | None, **kw: Any) -> tuple[Valuation, list[Metric]]:
    val = valuation(st, price, shares, **kw)
    return val, growth(st) + margins(st) + balance(st) + val.metrics
