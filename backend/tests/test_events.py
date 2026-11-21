"""Event calendar (2026-11-21): BLS / FRED / FOMC parsing, options expiry, merge, service with fake sources, routes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data.finnhub_fund import EarningsEvent
from app.events import model
from app.events.model import Event
from app.events.service import EventsService
from app.main import create_app
from app.timeutil import KST, NY
from app.user_settings import UserEvent
from tests.test_api import LOCAL, make_services

ICS = (
    "BEGIN:VCALENDAR\r\nPRODID:-//Department of Labor//Bureau of Labor Statistics//EN\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261210T083000\r\nDURATION:PT0M\r\nSUMMARY:Consumer Price Index\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20260710T083000\r\nSUMMARY:Consumer Price\r\n  Index\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261201T100000\r\nSUMMARY:Job Openings and Labor Turnover Survey\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261204T083000\r\nSUMMARY:Employment Situation\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261203T100000\r\nSUMMARY:County Employment and Wages\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n")

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=UTC).timestamp()       # Friday 2026-11-20 22:00 ET


# ── parsers ───────────────────────────────────────────────────────────────


def test_bls_ics_times_with_dst() -> None:
    ev = {e.id: e for e in model.parse_bls_ics(ICS)}
    cpi = ev["bls:Consumer Price Index:20261210"]
    assert cpi.at is not None and cpi.at.astimezone(UTC) == datetime(2026, 12, 10, 13, 30, tzinfo=UTC)   # EST
    assert cpi.at.astimezone(KST).strftime("%m-%d %H:%M") == "12-10 22:30"
    summer = ev["bls:Consumer Price Index:20260710"]                                   # folded SUMMARY line
    assert summer.at.astimezone(KST).strftime("%H:%M") == "21:30"                     # EDT: 12:30 UTC
    assert cpi.importance == "high" and cpi.title == "US CPI" and cpi.source == "BLS"
    assert ev["bls:Job Openings and Labor Turnover Survey:20261201"].at.astimezone(UTC).hour == 15
    assert not any("County" in i for i in ev)                                          # not a watched release


def test_fred_dates_are_date_only_and_future() -> None:
    raw = {"release_dates": [{"release_id": 53, "date": "2026-10-29"}, {"release_id": 53, "date": "2026-11-25"},
                             {"release_id": 53, "date": "bad"}]}
    out = model.parse_fred_dates(raw, 53, date(2026, 11, 21))
    assert [(e.day, e.at, e.title) for e in out] == [(date(2026, 11, 25), None, "US GDP")]
    assert model.parse_fred_dates("nope", 54, date(2026, 11, 21)) == []


def test_fomc_file_matches_fed_page() -> None:
    ev = model.load_fomc()
    assert len(ev) == 16 and all(e.at is None and e.kind == "fomc" for e in ev)
    dec = next(e for e in ev if e.day == date(2026, 12, 9))
    assert "projections" in dec.title and "sep" in dec.tags and "tentative" in (dec.note or "")
    assert [e.day for e in ev if e.day.year == 2027][0] == date(2027, 1, 27)
    assert not any("sep" in e.tags for e in ev if e.day.year == 2027)                  # page: no asterisks yet


def test_third_friday_and_opex() -> None:
    assert model.third_friday(2026, 11) == date(2026, 11, 20)
    assert model.third_friday(2026, 12) == date(2026, 12, 18)
    assert model.third_friday(2027, 1) == date(2027, 1, 15)
    ev = model.opex_events(date(2026, 11, 21), date(2027, 1, 31), closed={date(2027, 1, 15)})
    assert [(e.day, e.importance) for e in ev] == [(date(2026, 12, 18), "high"), (date(2027, 1, 14), "medium")]
    assert "quad" in ev[0].title and ev[0].at.astimezone(NY).hour == 16                # moved for the holiday ^


def test_merge_window_dedupe_and_order() -> None:
    start = datetime.fromtimestamp(NOW, UTC)
    past = Event("a", "macro", "past", "p", date(2026, 11, 20), datetime(2026, 11, 20, 13, 30, tzinfo=UTC), "x")
    today_date_only = Event("b", "macro", "today", "t", date(2026, 11, 20), None, "x")   # US day not over yet
    later = Event("c", "macro", "later", "l", date(2026, 11, 25), datetime(2026, 11, 25, 13, 30, tzinfo=UTC), "x")
    out = model.merge([later, past, today_date_only, later], start, start + timedelta(days=14))
    assert [e.id for e in out] == ["b", "c"]


def test_user_event_time_needs_offset_or_date() -> None:
    assert UserEvent(name="US election", time="2028-11-07").time == "2028-11-07"
    assert UserEvent(name="Talk", time="2026-12-01T20:00:00+09:00").time.endswith("+09:00")
    with pytest.raises(ValueError):
        UserEvent(name="x", time="2026-12-01T20:00:00")                                   # no offset: no guessing


# ── service with fake sources ─────────────────────────────────────────────


class FakeToss:
    def __init__(self) -> None:
        self.days: list[date] = []

    async def market_calendar(self, d: date) -> Any:
        self.days.append(d)
        if d == date(2026, 11, 26):                                                       # Thanksgiving
            return SimpleNamespace(today=SimpleNamespace(date=d, regular_market=None))
        close = (13, 0) if d == date(2026, 11, 27) else (16, 0)
        start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
        end = datetime(d.year, d.month, d.day, *close, tzinfo=NY)
        return SimpleNamespace(today=SimpleNamespace(date=d, regular_market=SimpleNamespace(start=start, end=end)))

    async def sectors(self) -> list[Any]:
        return [SimpleNamespace(sector_id="1", depth=0), SimpleNamespace(sector_id="2", depth=0),
                SimpleNamespace(sector_id="9", depth=1)]

    async def sector_stocks(self, sid: str, market: str) -> list[Any]:
        return {"1": [SimpleNamespace(symbol="AAPL", market_cap=Decimal("5e15")),
                      SimpleNamespace(symbol="SMALL", market_cap=Decimal("1e9"))],
                "2": [SimpleNamespace(symbol="WMT", market_cap=Decimal("1e15"))]}.get(sid, [])


class FakeFinnhub:
    configured = True

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def earnings_calendar(self, symbol: str, start: date, end: date) -> list[EarningsEvent]:
        self.asked.append(symbol)
        d = {"NVDA": date(2026, 11, 25), "AAPL": date(2026, 12, 3)}.get(symbol)
        return [] if d is None else [EarningsEvent(d, "amc", 2026, 3, None, None, None, None)]


def http_with(fred_status: int = 200) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if "bls.gov" in req.url.host:
            return httpx.Response(200, text=ICS)
        if "stlouisfed" in req.url.host:
            rid = int(req.url.params["release_id"])
            day = "2026-11-25" if rid == 53 else "2026-11-25"
            return httpx.Response(fred_status, json={"release_dates": [{"release_id": rid, "date": day}]})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def service(tmp_path: Path, fred_status: int = 200) -> tuple[EventsService, FakeFinnhub, FakeToss]:
    svc = make_services(tmp_path)
    svc.config.event_calendar.days = 30
    svc.config.event_calendar.top_market_cap = 2
    svc.config.event_calendar.big_semis = ["NVDA"]
    svc._fred_key = "fredkeyfortest000000000000000000"
    toss, fh = FakeToss(), FakeFinnhub()
    svc.toss = toss
    return EventsService(svc, http=http_with(fred_status), finnhub=fh, clock=lambda: NOW), fh, toss


@pytest.mark.anyio
async def test_service_merges_all_sources(tmp_path: Path) -> None:
    ev, fh, toss = service(tmp_path)
    await ev.refresh()
    out = ev.upcoming()
    ids = [e.id for e in out]
    assert ids.index("fred:53:20261125") < ids.index("mkt:closed:20261126") < ids.index("mkt:early:20261127")
    assert "bls:Employment Situation:20261204" in ids and "fomc:20261209" in ids and "opex:20261218" in ids
    assert "bls:Consumer Price Index:20261210" in ids
    early = next(e for e in out if e.id == "mkt:early:20261127")
    assert "13:00 ET" in early.title and early.at is not None
    earn = {e.symbol: e for e in out if e.kind == "earnings"}
    assert set(earn) == {"NVDA", "AAPL"} and earn["NVDA"].note == "big semiconductors"
    assert earn["AAPL"].note == "top 2 by market cap" and "after the close" in earn["AAPL"].title
    assert fh.asked[:1] == ["SOXS"] and "WMT" in fh.asked and "SMALL" not in fh.asked   # default ticker, top 2 caps
    assert all(d.weekday() < 5 for d in toss.days)
    nxt = ev.next_event()
    assert nxt is not None and nxt["name"] == "US GDP" and nxt["time"] is None and nxt["date"] == "2026-11-25"
    assert all(s["ok"] for s in ev.sources_view())


@pytest.mark.anyio
async def test_failing_source_keeps_others_and_hides_key(tmp_path: Path) -> None:
    ev, _, _ = service(tmp_path, fred_status=500)
    await ev.refresh()
    src = {s["name"]: s for s in ev.sources_view()}
    assert not src["fred"]["ok"] and src["fred"]["error"] == "http-500" and src["bls"]["ok"]
    assert "fredkey" not in str(src)
    assert not ev._due("fred")                                    # retried after 10 minutes, not on every request
    assert any(e.kind == "macro" for e in ev.upcoming())


@pytest.mark.anyio
async def test_user_events_from_settings_and_config(tmp_path: Path) -> None:
    ev, _, _ = service(tmp_path)
    ev.svc.user_settings = SimpleNamespace(events=SimpleNamespace(items=[
        UserEvent(name="US election", time="2026-12-01", note="general"),
        UserEvent(name="Earnings call I care about", time="2026-11-24T21:00:00+09:00")]))
    out = {e.title: e for e in ev.upcoming()}
    assert out["US election"].at is None and out["US election"].source == "my events"
    assert out["Earnings call I care about"].at.astimezone(KST).hour == 21


def test_events_route_and_strip(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        r = c.get("/api/events", params={"days": 40})
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 40 and {s["name"] for s in body["sources"]} >= {"bls", "fred", "earnings"}
        assert all(e["kind"] in ("opex", "user", "fomc", "macro", "market", "earnings") for e in body["events"])


@pytest.mark.anyio
async def test_chat_context_has_events_and_resends_when_they_change(tmp_path: Path) -> None:
    from app.events.service import events_of
    from tests.test_chat_engine import FakeClient, collect, make_engine, ready_services, usage

    svc = await ready_services(tmp_path)
    ev = events_of(svc)
    soon = datetime.now(UTC) + timedelta(days=1)
    ev.state["bls"].events = [Event("bls:cpi", "macro", "US CPI", "미국 CPI", soon.astimezone(NY).date(), soon, "BLS",
                                    "high")]
    client = FakeClient([([{"type": "text", "text": "ok"}], "end_turn", usage())] * 3)
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create()

    def contexts(call: int) -> list[str]:
        msg = client.calls[call]["messages"][-1]["content"]
        return [b["text"] for b in msg if b["type"] == "text" and b["text"].startswith("<context>")]

    await collect(eng, cid, "Q1")
    assert len(contexts(0)) == 1 and "upcoming_events_7d" in contexts(0)[0] and "US CPI" in contexts(0)[0]
    await collect(eng, cid, "Q2")
    assert contexts(1) == []                                          # unchanged: not sent again (cached history)
    later = soon + timedelta(days=1)
    ev.state["fred"].events = [Event("fred:53:x", "macro", "US GDP", "미국 GDP", later.astimezone(NY).date(), None,
                                     "FRED (BEA)", "high")]
    await collect(eng, cid, "Q3")
    assert len(contexts(2)) == 1 and "US GDP" in contexts(2)[0] and "date only" in contexts(2)[0]


def test_my_events_save_through_settings_merge() -> None:
    from app.user_settings import SettingsError, UserSettings, merge

    s = merge(UserSettings(), {"events": {"items": [{"name": "US election", "time": "2028-11-07", "note": None}]}})
    assert s.events.items[0].name == "US election"
    with pytest.raises(SettingsError):
        merge(UserSettings(), {"events": {"items": [{"name": "", "time": "2028-11-07"}]}})
    with pytest.raises(SettingsError):
        merge(UserSettings(), {"events": {"items": [{"name": "x", "time": "next tuesday"}]}})


def test_top_companies_merge_share_classes() -> None:
    from app.events.service import company_key, top_companies

    caps = {"AAPL": 10, "GOOG": 9, "GOOGL": 9, "BRK.A": 5, "BRK.B": 5, "MSFT": 8, "XYZA": 4, "XYZC": 3.9, "ANT": 3}
    names = {"XYZA": "Xyz Holdings Class A", "XYZC": "Xyz Holdings Class C", "ANT": "Anthropic"}
    assert top_companies(caps, names, 5) == ["AAPL", "GOOG", "MSFT", "BRK.A", "XYZA"]
    assert top_companies(caps, names, 30)[-1] == "ANT" and len(top_companies(caps, names, 30)) == 6
    assert company_key("AAPL", "Apple") != company_key("AMZN", "Amazon")
    assert company_key("BRK.B") == company_key("BRK.A")
    assert company_key("NVDA", "NVIDIA") == "sym:NVDA" and company_key("V", "Visa") == "sym:V"   # no letter cut
    assert company_key("GOOGL", "알파벳 A") == company_key("GOOG", "알파벳 C")
    assert company_key("X1", "Foo (Class B)") == company_key("X2", "Foo Class A") == "name:foo"


@pytest.mark.anyio
async def test_ticker_symbols_route(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    await svc.refresh_stocks()
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        body = c.get("/api/tickers/symbols").json()
        assert body["count"] == len(body["symbols"]) > 0 and "SOXS" in body["symbols"]
        assert body["symbols"] == sorted(body["symbols"])
