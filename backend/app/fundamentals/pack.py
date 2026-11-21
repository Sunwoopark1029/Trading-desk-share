"""The fundamentals pack (phase 6 part B): one object with every fundamental value, read by the screen and the AI.

Each value has a unique id (ttm.revenue, fy2025.net_income, rev_cagr_3y, pe_ttm, fv_pe_own_median, target_mean ...),
its exact value, a display string made here ("USD 35.1B", "23.4x", "+18.2%", "USD 245.13"), unit, period, source
and as-of time, plus a note when it is missing or estimated. The AI may only quote these display strings / values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.fundamentals.concepts import LINES
from app.fundamentals.metrics import Metric
from app.fundamentals.statements import Statements

SCALES = ((Decimal(10) ** 12, "T"), (Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M"))


def _q(v: Decimal, places: int) -> Decimal:
    return v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _scaled(a: Decimal, places: int) -> str:
    """a >= 0 -> "35.1B". The scale is picked after rounding, so 999.95M shows as 1.0B, never 1000.0M."""
    for i, (scale, suffix) in enumerate(SCALES):
        if a >= scale:
            q = _q(a / scale, places)
            if q >= 1000 and i > 0:
                scale, suffix = SCALES[i - 1]
                q = _q(a / scale, places)
            return f"{q}{suffix}"
    q = _q(a, 0)
    if q >= SCALES[-1][0]:                               # 999,999.6 rounds up to a million
        return f"{_q(a / SCALES[-1][0], places)}{SCALES[-1][1]}"
    return f"{q:,}"


def fmt_money(v: Decimal, currency: str) -> str:
    return f"{'−' if v < 0 else ''}{currency} {_scaled(abs(v), 1)}"


def fmt_pct(v: Decimal) -> str:
    p = _q(v * 100, 1)
    return f"{'+' if p > 0 else '−' if p < 0 else ''}{abs(p)}%"


def fmt_level(v: Decimal) -> str:
    """A level such as a margin or yield: no plus sign ("48.8%", "−3.2%")."""
    p = _q(v * 100, 1)
    return f"{'−' if p < 0 else ''}{abs(p)}%"


def fmt_multiple(v: Decimal) -> str:
    q = _q(v, 1)
    return f"{'−' if q < 0 else ''}{abs(q)}x"


def fmt_per_share(v: Decimal, currency: str) -> str:
    return f"{'−' if v < 0 else ''}{currency} {_q(abs(v), 2)}"


def fmt_shares(v: Decimal) -> str:
    return f"{'−' if v < 0 else ''}{_scaled(abs(v), 2)} shares"


def display(value: Decimal | None, kind: str, currency: str | None, note: str | None) -> str:
    if value is None:
        return note or "not reported"
    if kind == "money":
        return fmt_money(value, currency or "?")
    if kind == "pct":
        return fmt_pct(value)
    if kind == "level":
        return fmt_level(value)
    if kind == "multiple":
        return fmt_multiple(value)
    if kind == "per_share":
        return fmt_per_share(value, currency or "?")
    if kind == "shares":
        return fmt_shares(value)
    return str(_q(value, 4))


@dataclass(frozen=True)
class PackValue:
    id: str
    label: str
    value: Decimal | None
    display: str
    kind: str
    period: str
    source: str
    as_of: str | None
    currency: str | None = None
    note: str | None = None
    basis: tuple[str, ...] = ()
    live: bool = False                # moves with the live price (market cap, multiples, implied prices)


@dataclass
class Pack:
    symbol: str
    entity: str | None
    currency: str                     # statements currency
    values: list[PackValue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ids(self) -> list[str]:
        return [v.id for v in self.values]

    def get(self, vid: str) -> PackValue | None:
        return next((v for v in self.values if v.id == vid), None)

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "entity": self.entity, "currency": self.currency,
                "warnings": list(self.warnings), "values": [asdict(v) for v in self.values]}


def build_pack(symbol: str, st: Statements, metrics: list[Metric], *, statements_source: str = "SEC EDGAR",
               statements_as_of: str | None = None, price_source: str = "Toss", price_as_of: str | None = None,
               history_price_source: str = "Toss", peers_source: str = "Toss sector peers + SEC EDGAR",
               peers_as_of: str | None = None, extra: list[PackValue] | None = None) -> Pack:
    """Statement lines for TTM + each fiscal year, every metric, plus extra values (targets, earnings ...)."""
    pk = Pack(symbol.upper(), st.entity, st.currency, warnings=list(st.warnings))
    kinds = {"money": "money", "per_share": "per_share", "shares": "shares"}
    periods = ([("TTM", "ttm", st.ttm.label)] if st.ttm is not None else []) + \
        [(p.key, p.key.lower(), p.label) for p in st.annual]
    for key, tag, label in periods:
        for line in LINES:
            cell = st.rows.get(line.id, {}).get(key)
            if cell is None:
                continue
            kind = kinds[line.measure]
            cur = st.currency if kind in ("money", "per_share") else None
            note = cell.note if cell.value is None else ("derived" if cell.derived else None)
            pk.values.append(PackValue(f"{tag}.{line.id}", f"{line.label} ({label})", cell.value,
                                       display(cell.value, kind, cur, cell.note), kind, key, statements_source,
                                       statements_as_of, cur, note, (cell.concept,) if cell.concept else ()))
    for m in metrics:
        live = False
        if m.period == "history":                        # own-history median: statements + fiscal-year-end prices
            src, as_of = f"calculated ({statements_source} + {history_price_source} fiscal-year-end prices)",                 statements_as_of
        elif m.period == "peers":
            src, as_of = f"calculated (peer median: {peers_source})", peers_as_of
        elif m.period == "live" or m.id in ("mcap", "ev") or m.id.startswith(("pe_ttm", "ps_", "pb", "ev_",
                                                                              "fcf_yield", "fv_")):
            src, as_of = f"calculated ({statements_source} + {price_source})", price_as_of
            live = True
        else:
            src, as_of = f"calculated ({statements_source})", statements_as_of
        pk.values.append(PackValue(m.id, m.label, m.value, display(m.value, m.kind, m.currency, m.note), m.kind,
                                   m.period, src, as_of, m.currency, m.note, m.basis, live))
    for v in extra or []:
        pk.values.append(v)
    seen: set[str] = set()
    dup = [i for i in pk.ids() if i in seen or seen.add(i)]   # type: ignore[func-returns-value]
    if dup:
        raise ValueError(f"duplicate pack ids: {sorted(set(dup))}")
    return pk
