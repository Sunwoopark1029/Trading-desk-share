"""Company view data (phase 6 part B): statements, metrics pack, earnings, analysts, peers, news for one ticker.

Fetched when a company is opened, cached briefly (config `research:`), errors keep the last good value and are shown
as a status with a short code (never a URL, key or contact address). Sources:
- SEC EDGAR companyfacts -> statement tables (statements.py); yfinance as the labelled last fallback when SEC has
  nothing for a company. ETFs (Toss securityType ETF / FOREIGN_ETF / ETN) have no company statements: said plainly.
- Toss: live price, shares outstanding (stock basics), daily candles for fiscal-year-end prices, sector stock lists.
- Finnhub: profile, last 4 quarters of EPS surprises, next earnings date, rating trends (one app-wide gate).
- yfinance: analyst price targets (unofficial).
All numbers are calculated in code (metrics.py / pack.py); this module only joins and labels them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.data.edgar import EdgarClient, EdgarError
from app.data.finnhub_fund import FinnhubError, FinnhubFund, gate_of
from app.data.toss import StockInfo
from app.data.yf_fund import YFFund, YFFundError, YFStatements
from app.fundamentals.concepts import LINES
from app.fundamentals.metrics import Metric, all_metrics, implied, own_history_pe
from app.fundamentals.pack import PackValue, build_pack, display
from app.fundamentals.peers import peer_medians, pick_peers
from app.fundamentals.statements import Cell, Period, Statements, build
from app.research.sector_index import SectorIndex
from app.services import InputError, Services, epoch_kst, error_info
from app.timeutil import KST

log = logging.getLogger("desk.research")
ETF_TYPES = ("ETF", "FOREIGN_ETF", "ETN")
PEER_METRICS = ("pe_ttm", "ps_ttm", "ev_ebit_ttm", "fcf_yield_ttm", "rev_yoy", "gross_margin_ttm", "op_margin_ttm",
                "net_margin_ttm", "roe")


def _code(exc: BaseException) -> tuple[str, str]:
    """Short code + safe message for any source error."""
    if isinstance(exc, (EdgarError, FinnhubError)):
        return exc.code, f"{'SEC' if isinstance(exc, EdgarError) else 'Finnhub'} error: {exc.code}"
    if isinstance(exc, YFFundError):
        return exc.code, f"yfinance error: {exc.code}"
    if isinstance(exc, InputError):                  # e.g. "Toss keys not set in .env" (safe to show)
        return "no-key", str(exc)
    return error_info(exc)


@dataclass
class Entry:
    value: Any
    at: float | None              # wall-clock time of the last good value
    error: str | None = None
    code: str | None = None


class Memo:
    """Per-key TTL cache with one loader per key at a time; failures keep the last good value."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._d: dict[str, Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str, ttl: float, load: Callable[[], Awaitable[Any]]) -> Entry:
        e = self._d.get(key)
        if e is not None and e.error is None and e.at is not None and self.clock() - e.at < ttl:
            return e
        async with self._locks.setdefault(key, asyncio.Lock()):
            e = self._d.get(key)
            if e is not None and e.error is None and e.at is not None and self.clock() - e.at < ttl:
                return e
            try:
                e = Entry(await load(), self.clock())
            except Exception as exc:  # noqa: BLE001 - shown as status error with the last good value
                code, msg = _code(exc)
                log.info("research %s failed: %s", key.split(":")[0], code)
                old = self._d.get(key)
                e = Entry(old.value if old else None, old.at if old else None, msg, code)
            self._d[key] = e
            return e


def meta(e: Entry | None, source: str, delay: str = "") -> dict[str, Any]:
    if e is None:
        return {"status": "pending", "source": source, "updated": None, "error": None, "error_code": None}
    return {"status": "error" if e.error else "ok", "source": source, "delay": delay,
            "updated": epoch_kst(e.at) if e.at else None, "error": e.error, "error_code": e.code}


class ResearchService:
    def __init__(self, svc: Services, *, edgar: EdgarClient | None = None, fund: FinnhubFund | None = None,
                 yf: YFFund | None = None, index: SectorIndex | None = None) -> None:
        from app.settings import CACHE_DIR, get_secrets

        self.svc = svc
        self.cfg = svc.config.research
        if edgar is None or fund is None:
            sec = get_secrets()
            e = svc.config.edgar
            edgar = edgar or EdgarClient(sec.SEC_USER_AGENT.get_secret_value(), CACHE_DIR / "edgar",
                                         requests_per_second=e.requests_per_second,
                                         facts_ttl_s=e.facts_ttl_hours * 3600, tickers_ttl_s=e.tickers_ttl_hours * 3600,
                                         cooldown_s=e.cooldown_minutes * 60, timeout_s=e.timeout_seconds)
            fund = fund or FinnhubFund(sec.FINNHUB_API_KEY.get_secret_value(), gate_of(svc))
        self.edgar, self.fund = edgar, fund
        self.yf = yf or YFFund()
        self.index = index or SectorIndex(svc, CACHE_DIR / "sector_index.json",
                                          self.cfg.sector_index_hours * 3600, svc.clock)
        self.memo = Memo(svc.clock)

    async def aclose(self) -> None:
        for c in (self.edgar, self.fund):
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ── shared inputs ────────────────────────────────────────────────────

    def _symbol(self, raw: str) -> str:
        return self.svc.validate_symbol((raw or "").strip().upper())

    async def basics(self, sym: str) -> StockInfo | None:
        from app.explore.views import explore_of
        return (await explore_of(self.svc).names([sym])).get(sym)

    async def price(self, sym: str) -> Entry:
        async def load() -> Any:
            if self.svc.toss is None:
                raise InputError("Toss keys not set in .env")
            got = await self.svc.toss.prices([sym])
            return got[0] if got else None
        return await self.memo.get(f"price:{sym}", self.svc.config.polling.quotes_seconds, load)

    def _sec_type(self, sym: str, info: StockInfo | None) -> str | None:
        """Security type from Toss stock basics, else from the daily Toss stock list."""
        if info is not None:
            return info.security_type
        listed = (getattr(self.svc, "stocks", None) or {}).get(sym)
        return listed.security_type if listed else None

    async def statements(self, sym: str, info: StockInfo | None) -> tuple[Statements | None, dict[str, Any]]:
        """SEC tables; the yfinance fallback only when SEC does not cover the company; ETFs get none."""
        if self._sec_type(sym, info) in ETF_TYPES:
            return None, {"status": "none", "source": None, "reason": "etf",
                          "note": "ETF: no company statements (see the ETF details instead)"}

        async def load_sec() -> Any:
            entry = await self.edgar.cik_for(sym)
            if entry is None:
                return None
            res = await self.edgar.company_facts(entry.cik)
            st = await asyncio.to_thread(build, res.data, self.cfg.years + 1, self.cfg.quarters)
            return entry.cik, st, res.fetched_at, res.stale, res.error_code    # the raw multi-MB facts are dropped

        e = await self.memo.get(f"sec:{sym}", 3600, load_sec)
        if e.value is not None:
            cik, st, fetched_at, stale, err = e.value
            m = {"status": "stale" if stale else "ok", "source": "SEC EDGAR", "cik": cik,
                 "updated": epoch_kst(fetched_at), "error": e.error or (f"SEC error: {err}" if stale else None)}
            return st, m
        if e.error and e.code not in ("not-found",):          # SEC is failing: say so, never swap in yfinance
            return None, {"status": "error", "source": "SEC EDGAR", "error": e.error, "error_code": e.code,
                          "reason": "sec-error"}
        sec_meta = {"status": "none", "source": "SEC EDGAR", "note": "not in SEC's data"}
        y = await self.memo.get(f"yfst:{sym}", self.cfg.history_ttl_seconds, lambda: self.yf.statements(sym))
        if isinstance(y.value, YFStatements) and y.value.lines:
            return from_yf(y.value, info), {"status": "ok", "source": y.value.source, "fallback": True,
                                            "sec": sec_meta, "updated": epoch_kst(y.at) if y.at else None,
                                            "note": "SEC has no data for this company: unofficial yfinance figures"}
        return None, {**sec_meta, "reason": "none", "fallback_error": y.error}

    async def year_end_prices(self, sym: str, ends: list[date]) -> tuple[dict[date, Decimal], Entry]:
        """Last Toss daily close on or before each fiscal-year end (within 7 days)."""
        async def load() -> Any:
            if self.svc.toss is None:
                raise InputError("Toss keys not set in .env")
            return await self.svc.toss.candles_history(sym, "1d", total=(self.cfg.years + 2) * 260)

        e = await self.memo.get(f"hist:{sym}", self.cfg.history_ttl_seconds, load)
        out: dict[date, Decimal] = {}
        bars = sorted(((c.timestamp.astimezone(KST).date(), c.close) for c in (e.value or [])), key=lambda t: t[0])
        for end in ends:
            before = [(d, px) for d, px in bars if end - timedelta(days=7) <= d <= end]
            if before:
                out[end] = before[-1][1]
        return out, e

    # ── views ────────────────────────────────────────────────────────────

    async def overview(self, raw: str) -> dict[str, Any]:
        sym = self._symbol(raw)
        self.index.ensure()
        info = await self.basics(sym)
        prof = await self.memo.get(f"profile:{sym}", self.cfg.profile_ttl_seconds, lambda: self.fund.profile(sym))
        today = datetime.now(UTC).astimezone(KST).date()
        cal = await self.memo.get(f"cal:{sym}", self.cfg.earnings_ttl_seconds,
                                  lambda: self.fund.earnings_calendar(sym, today - timedelta(days=30),
                                                                      today + timedelta(days=120)))
        upcoming = [ev for ev in (cal.value or []) if ev.date is not None and ev.date >= today]
        px = await self.price(sym)
        sector = self.index.sector_for(sym)
        stype = self._sec_type(sym, info)
        kind = "etf" if stype in ETF_TYPES else "depositary_receipt" if stype == "DEPOSITARY_RECEIPT" else "stock"
        return {
            "symbol": sym, "kind": kind,
            "basics": None if info is None else {
                "name": info.name, "english_name": info.english_name, "market": info.market,
                "security_type": info.security_type, "status": info.status, "currency": info.currency,
                "list_date": info.list_date, "shares_outstanding": info.shares_outstanding,
                "shares_note": "Toss figure; for depositary receipts it looks like the ADS count (unverified)"
                if kind == "depositary_receipt" else None,
                "leverage_factor": info.leverage_factor},
            "profile": None if prof.value is None else {
                "name": prof.value.name, "country": prof.value.country, "filing_currency": prof.value.currency,
                "exchange": prof.value.exchange, "ipo": prof.value.ipo, "industry": prof.value.industry,
                "logo": prof.value.logo, "weburl": prof.value.weburl},
            "next_earnings": None if not upcoming else {
                "date": upcoming[0].date, "hour": upcoming[0].hour, "eps_estimate": upcoming[0].eps_estimate,
                "revenue_estimate": upcoming[0].revenue_estimate, "currency": "USD",
                "note": "Finnhub: EPS / revenue are non-GAAP; USD assumed (no currency in the spec)"},
            "price": None if px.value is None else {"last": px.value.last_price, "currency": px.value.currency,
                                                    "time": px.value.timestamp},
            "sector": None if sector is None else {"sector_id": sector.sector_id, "name": sector.name,
                                                   "depth": sector.depth},
            "sector_index": self.index.meta(),
            "meta": {"profile": meta(prof, "Finnhub"), "calendar": meta(cal, "Finnhub"),
                     "price": meta(px, "Toss", "real-time")},
        }

    async def financials(self, raw: str, freq: str = "annual", years: int | None = None) -> dict[str, Any]:
        sym = self._symbol(raw)
        if freq not in ("annual", "quarterly"):
            raise InputError("freq must be annual or quarterly")
        info = await self.basics(sym)
        st, m = await self.statements(sym, info)
        if st is None:
            return {"symbol": sym, "freq": freq, "table": None, "meta": m}
        n = min(years or self.cfg.years, self.cfg.years)
        periods = st.annual[:n] if freq == "annual" else st.quarterly[: self.cfg.quarters]
        return {"symbol": sym, "freq": freq, "table": table_json(st, periods), "meta": m}

    async def metrics(self, raw: str) -> dict[str, Any]:
        sym = self._symbol(raw)
        info = await self.basics(sym)
        st, smeta = await self.statements(sym, info)
        if st is None:
            return {"symbol": sym, "pack": None, "meta": {"statements": smeta}}
        px = await self.price(sym)
        price = px.value.last_price if px.value is not None else None
        pcur = px.value.currency if px.value is not None else "USD"
        shares = info.shares_outstanding if info else None
        val, ms = all_metrics(st, price, shares, price_currency=pcur, security_type=self._sec_type(sym, info),
                              price_time=str(px.value.timestamp) if px.value is not None else None)
        prices, hist = await self.year_end_prices(sym, [p.end for p in st.annual])
        own, cut = own_history_pe(st, prices)
        if hist.error and not prices:
            cut = "; ".join(x for x in (cut, f"year-end prices unavailable ({hist.error})") if x)
        peer_pe, peer_at = self._cached_peer_median(sym)
        ms += implied(st, val, (own, cut), peer_pe)
        extra = await self._analyst_values(sym)
        pk = build_pack(sym, st, ms, statements_source=smeta.get("source") or "SEC EDGAR",
                        statements_as_of=smeta.get("updated"), price_as_of=meta(px, "Toss")["updated"],
                        peers_as_of=epoch_kst(peer_at) if peer_at else None, extra=extra)
        return {"symbol": sym, "pack": pk.as_dict(),
                "meta": {"statements": smeta, "price": meta(px, "Toss", "real-time"),
                         "history": meta(hist, "Toss daily candles"),
                         "notes": ["Toss daily prices: split adjustment unverified"]}}

    async def _analyst_values(self, sym: str) -> list[PackValue]:
        t = await self.memo.get(f"targets:{sym}", self.cfg.earnings_ttl_seconds, lambda: self.yf.price_targets(sym))
        out: list[PackValue] = []
        if t.value is not None:
            for k in ("low", "median", "mean", "high"):
                v = getattr(t.value, k)
                out.append(PackValue(f"target_{k}", f"Analyst target ({k})", v,
                                     display(v, "per_share", t.value.currency, "not reported"), "per_share", "latest",
                                     t.value.source, epoch_kst(t.at) if t.at else None, t.value.currency,
                                     "USD assumed (US listing)"))
        return out

    async def earnings(self, raw: str) -> dict[str, Any]:
        sym = self._symbol(raw)
        e = await self.memo.get(f"eps:{sym}", self.cfg.earnings_ttl_seconds, lambda: self.fund.earnings(sym))
        today = datetime.now(UTC).astimezone(KST).date()
        cal = await self.memo.get(f"cal:{sym}", self.cfg.earnings_ttl_seconds,
                                  lambda: self.fund.earnings_calendar(sym, today - timedelta(days=30),
                                                                      today + timedelta(days=120)))
        info = await self.basics(sym)
        st, smeta = await self.statements(sym, info)
        history = []
        if st is not None:
            for p in st.quarterly:
                c = st.rows["eps_diluted"].get(p.key)
                history.append({"period": p.label, "end": p.end, "eps_diluted": c.value if c else None,
                                "currency": st.currency, "note": c.note if c else None})
        return {"symbol": sym,
                "surprises": [{"period": s.period, "year": s.year, "quarter": s.quarter, "actual": s.actual,
                               "estimate": s.estimate, "surprise": s.surprise, "surprise_pct": s.surprise_pct,
                               "currency": "USD"} for s in (e.value or [])],
                "calendar": [{"date": c.date, "hour": c.hour, "eps_estimate": c.eps_estimate,
                              "eps_actual": c.eps_actual, "revenue_estimate": c.revenue_estimate,
                              "revenue_actual": c.revenue_actual, "currency": "USD"} for c in (cal.value or [])],
                "currency_note": "USD assumed (US listing; Finnhub's spec gives no currency)",
                "sec_eps_history": history,
                "notes": ["Finnhub free plan: last 4 quarters of EPS surprises",
                          "Finnhub calendar EPS / revenue are non-GAAP (spec); whether the EPS-surprise figures are "
                          "GAAP or non-GAAP is not stated (unverified); SEC EPS is as reported (GAAP)"],
                "meta": {"surprises": meta(e, "Finnhub"), "calendar": meta(cal, "Finnhub"), "statements": smeta}}

    async def analysts(self, raw: str) -> dict[str, Any]:
        sym = self._symbol(raw)
        r = await self.memo.get(f"rec:{sym}", self.cfg.earnings_ttl_seconds, lambda: self.fund.recommendations(sym))
        t = await self.memo.get(f"targets:{sym}", self.cfg.earnings_ttl_seconds, lambda: self.yf.price_targets(sym))
        px = await self.price(sym)
        tv = t.value
        return {"symbol": sym,
                "ratings": [{"period": x.period, "strong_buy": x.strong_buy, "buy": x.buy, "hold": x.hold,
                             "sell": x.sell, "strong_sell": x.strong_sell} for x in (r.value or [])],
                "targets": None if tv is None else {"low": tv.low, "median": tv.median, "mean": tv.mean,
                                                    "high": tv.high, "current": tv.current, "currency": tv.currency,
                                                    "source": tv.source, "currency_note": "USD assumed (US listing)"},
                "price": None if px.value is None else {"last": px.value.last_price, "currency": px.value.currency},
                "meta": {"ratings": meta(r, "Finnhub"), "targets": meta(t, "yfinance · unofficial"),
                         "price": meta(px, "Toss", "real-time")}}

    # ── peers ────────────────────────────────────────────────────────────

    def _cached_peer_median(self, sym: str) -> tuple[Decimal | None, float | None]:
        """Peer P/E median from the peers view if computed, current (within its TTL) and not failed."""
        e = self.memo._d.get(f"peers:{sym}")
        if e is None or e.value is None or e.error is not None or e.at is None:
            return None, None
        if self.svc.clock() - e.at >= self.cfg.peers_ttl_seconds:
            return None, None
        med = next((m for m in e.value["medians"] if m["metric_id"] == "pe_ttm"), None)
        return (med["value"], e.at) if med and med["value"] is not None else (None, None)

    async def _peer_row(self, peer: str) -> dict[str, Any]:
        info = await self.basics(peer)
        st, smeta = await self.statements(peer, info)
        row: dict[str, Any] = {"symbol": peer, "name": info.name if info else None,
                               "english_name": info.english_name if info else None, "source": smeta.get("source")}
        if st is None:
            row["metrics"] = {k: None for k in PEER_METRICS}
            row["note"] = smeta.get("note") or smeta.get("error")
            return row
        px = await self.price(peer)
        _, ms = all_metrics(st, px.value.last_price if px.value else None, info.shares_outstanding if info else None,
                            price_currency=px.value.currency if px.value else "USD",
                            security_type=self._sec_type(peer, info))
        by = {m.id: m for m in ms}
        row["metrics"] = {k: by[k].value if k in by else None for k in PEER_METRICS}
        row["display"] = {k: display(by[k].value, by[k].kind, by[k].currency, by[k].note) if k in by else "—"
                          for k in PEER_METRICS}
        return row

    async def peers(self, raw: str) -> dict[str, Any]:
        sym = self._symbol(raw)
        self.index.ensure()
        sector = self.index.sector_for(sym)
        if sector is None:
            return {"symbol": sym, "sector": None, "peers": [], "medians": [], "sector_index": self.index.meta(),
                    "note": "sector index is still being built" if self.index.status == "building"
                    else "this ticker is in no Toss sector list"}

        async def load() -> Any:
            sec_tickers = await self.edgar.tickers() if self.edgar.configured else {}
            allowed = {t.replace("-", ".") for t in sec_tickers} | set(sec_tickers) if sec_tickers else None
            cik_of = {**{t: v.cik for t, v in sec_tickers.items()},
                      **{t.replace("-", "."): v.cik for t, v in sec_tickers.items()}}
            seen = {cik_of[sym]} if sym in cik_of else set()
            cands = []
            for s in self.index.members.get(sector.sector_id, []):
                c = cik_of.get(s.upper())
                if c is not None:
                    if c in seen:
                        continue                         # same company (another share class) as self or a peer
                    seen.add(c)
                cands.append(s)
            names, note = pick_peers(cands, sym, self.cfg.peers, allowed)
            rows = [await self._peer_row(p) for p in names]
            medians = peer_medians({r["symbol"]: r["metrics"] for r in rows}, PEER_METRICS)
            return {"peers": rows, "medians": [{"metric_id": m.metric_id, "value": m.value, "count": m.count,
                                                 "of": m.of, "note": m.note} for m in medians], "note": note}

        e = await self.memo.get(f"peers:{sym}", self.cfg.peers_ttl_seconds, load)
        v = e.value or {"peers": [], "medians": [], "note": None}
        return {"symbol": sym, "sector": {"sector_id": sector.sector_id, "name": sector.name, "depth": sector.depth},
                **v, "sector_index": self.index.meta(), "meta": meta(e, "Toss sector + SEC EDGAR + Toss prices")}

    # ── news ─────────────────────────────────────────────────────────────

    async def news(self, raw: str, days: int | None = None) -> dict[str, Any]:
        from app.news.feed import dedupe, normalize_finnhub

        sym = self._symbol(raw)
        days = max(1, min(days or self.cfg.news_days, self.cfg.news_days))

        async def load() -> Any:
            now = self.svc.clock()
            end = datetime.fromtimestamp(now, UTC).date() + timedelta(days=1)
            raw_items = await self.fund.company_news(sym, end - timedelta(days=days + 1), end)
            return dedupe([x for x in (normalize_finnhub(r, sym, now) for r in raw_items) if x is not None])

        e = await self.memo.get(f"news:{sym}:{days}", self.cfg.news_ttl_seconds, load)
        feed = getattr(self.svc, "news", None)
        tags = {i.id: i.tag for i in feed.items({sym})} if feed is not None else {}
        items = sorted(e.value or [], key=lambda i: (i.published or i.first_seen), reverse=True)
        return {"symbol": sym, "days": days,
                "items": [{**i.view(), "tag": tags.get(i.id) or i.tag} for i in items[:100]],
                "meta": meta(e, "Finnhub company-news")}


def table_json(st: Statements, periods: list[Period]) -> dict[str, Any]:
    rows = []
    for line in LINES:
        cells = {p.key: _cell(st.rows.get(line.id, {}).get(p.key)) for p in periods}
        if all(c["value"] is None for c in cells.values()):
            continue                                  # a line this company never reports: left out of the table
        rows.append({"id": line.id, "label": line.label, "statement": line.statement, "measure": line.measure,
                     "cells": cells})
    ttm = st.rows and st.ttm
    return {"currency": st.currency, "entity": st.entity, "taxonomy": st.taxonomy, "warnings": st.warnings,
            "latest_filed": st.latest_filed,
            "periods": [{"key": p.key, "label": p.label, "kind": p.kind, "end": p.end} for p in periods],
            "ttm": None if not ttm else {"label": st.ttm.label,  # type: ignore[union-attr]
                                         "cells": {line.id: _cell(st.rows[line.id].get("TTM")) for line in LINES}},
            "rows": rows}


def _cell(c: Cell | None) -> dict[str, Any]:
    if c is None:
        return {"value": None, "note": "not reported", "derived": False, "concept": None}
    return {"value": c.value, "note": c.note, "derived": c.derived, "concept": c.concept, "form": c.form,
            "filed": c.filed}


def from_yf(y: YFStatements, info: StockInfo | None) -> Statements:
    """yfinance fallback lines -> a Statements object (annual only, TTM = latest fiscal year, labelled unofficial)."""
    ends: list[date] = []
    for d in sorted({d for vals in y.lines.values() for d in vals}, reverse=True):
        if all(d.year != e.year for e in ends):          # one column per calendar year: the newest year-end
            ends.append(d)
    periods = [Period(f"FY{d.year}", "annual", d.year, None, None, d, f"FY{d.year}, ended {d.isoformat()}")
               for d in ends]
    rows: dict[str, dict[str, Cell]] = {}
    for line in LINES:
        vals = y.lines.get(line.id, {})
        rows[line.id] = {p.key: (Cell(vals[p.end], "yfinance", note="unofficial") if p.end in vals
                                 else Cell(None, note="not reported")) for p in periods}
    ttm = None
    if periods:
        p = periods[0]
        ttm = Period("TTM", "ttm", p.fiscal_year, None, None, p.end, f"Latest fiscal year (FY{p.fiscal_year}), yfinance")
        for line in LINES:
            c = rows[line.id][p.key]
            rows[line.id]["TTM"] = Cell(c.value, c.concept, note="latest fiscal year (yfinance, unofficial)"
                                        if c.value is not None else c.note)
    return Statements(y.currency or "?", periods, [], ttm, rows,
                      ["Unofficial yfinance figures (SEC has no data for this company)"]
                      + ([] if y.currency else ["Reporting currency unknown"]),
                      None, info.english_name if info else None, "yfinance")


def research_of(svc: Services) -> ResearchService:
    r = getattr(svc, "_research", None)
    if r is None:
        r = ResearchService(svc)
        svc._research = r  # type: ignore[attr-defined]
    return r
