"""Options-implied context (plan step 2, 2026-11-21): calculations + fetcher normalization + cache."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from app.data.yf_options import OptionsService, contracts_from_rows
from app.indicators import options as o
from app.indicators.options import Chain, Contract, Snapshot
from app.timeutil import KST

EXP = date(2026, 12, 18)                       # EST: expires 2026-12-18 21:00 UTC
NOW = o.expiry_epoch(EXP) - 30 * 86400         # exactly 30 days before expiry
T30 = 30 / 365


def k(sym: str, exp: date, cp: str, strike: float) -> str:
    return f"{sym}{exp:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def c(sym: str, exp: date, cp: str, strike: float, bid: float | None, ask: float | None, *,
      vol: float | None = 0, oi: float | None = None, iv: float | None = None) -> Contract:
    return Contract(k(sym, exp, cp, strike), strike, bid, ask, None, vol, oi, iv, None)


def chain(exp: date, straddle_half: float, *, spread: float = 0.2, sym: str = "TEST", oi: float | None = None,
          vol: float = 0) -> Chain:
    """Strikes 80..120 step 5 around price 100; ATM call / put mid = straddle_half each; OTM legs cheaper."""
    calls, puts = [], []
    for s in range(80, 125, 5):
        cm = max(0.05, straddle_half - (s - 100) * 0.5) if s >= 100 else straddle_half + (100 - s) * 0.6
        pm = max(0.05, straddle_half - (100 - s) * 0.4) if s <= 100 else straddle_half + (s - 100) * 0.6
        calls.append(c(sym, exp, "C", s, cm - spread / 2, cm + spread / 2, vol=vol, oi=oi, iv=0.6))
        puts.append(c(sym, exp, "P", s, pm - spread / 2, pm + spread / 2, vol=vol, oi=oi, iv=0.4))
    return Chain(exp, tuple(calls), tuple(puts))


def half_for(iv: float, t: float, price: float = 100.0) -> float:
    return o.SQRT_2_OVER_PI * price * iv * math.sqrt(t) / 2


# ── time ──────────────────────────────────────────────────────────────────


def test_expiry_is_4pm_new_york_with_dst() -> None:
    assert datetime.fromtimestamp(o.expiry_epoch(date(2026, 11, 27)), UTC) == datetime(2026, 11, 27, 21, 0, tzinfo=UTC)
    assert datetime.fromtimestamp(o.expiry_epoch(date(2026, 7, 17)), UTC) == datetime(2026, 7, 17, 20, 0, tzinfo=UTC)
    kst = datetime.fromtimestamp(o.expiry_epoch(date(2026, 11, 27)), UTC).astimezone(KST)
    assert (kst.day, kst.hour) == (28, 6)                       # Saturday 06:00 KST
    assert o.years_to_expiry(EXP, NOW) == pytest.approx(T30)
    assert o.years_to_expiry(EXP, o.expiry_epoch(EXP) + 60) == 0.0


# ── filtering, straddle, IV ───────────────────────────────────────────────


def test_adjusted_contracts_are_dropped() -> None:
    std = Contract("SOXX261127C00123000", 123.0, 1, 2, None, 0, None, None, None)
    adj = Contract("SOXX1261127C00123330", 123.33, 1, 2, None, 0, None, None, None)
    other = Contract("SOXL261127C00123000", 123.0, 1, 2, None, 0, None, None, None)
    same_root = Contract("SOXX261127C00123330", 123.33, 1, 2, None, 0, None, None, None)   # seen live 2026-11-21
    half = Contract("SOXX261127C00122500", 122.5, 1, 2, None, 0, None, None, None)
    assert o.standard(std, "SOXX") and not o.standard(adj, "SOXX") and not o.standard(other, "SOXX")
    assert not o.standard(same_root, "SOXX") and o.standard(half, "SOXX")
    ch = o.clean(Chain(date(2026, 11, 27), (std, adj, other, same_root, half), ()), "soxx")
    assert [x.strike for x in ch.calls] == [123.0, 122.5]


def test_atm_straddle_needs_two_sided_quotes() -> None:
    ch = chain(EXP, 5.0)
    st = o.atm_straddle(ch, 101.0)
    assert st is not None and st.strike == 100 and st.mid == pytest.approx(10.0)
    assert st.spread_pct == pytest.approx(0.4 / 10.0)
    assert st.yahoo_iv == pytest.approx(0.5)                     # mean of call 0.6 / put 0.4 (cross-check)
    # ATM put has no bid -> the next strike with both sides quoted is used
    puts = tuple(p if p.strike != 100 else Contract(p.contract, 100, None, p.ask, None, 0, None, None, None)
                 for p in ch.puts)
    st2 = o.atm_straddle(Chain(EXP, ch.calls, puts), 101.0)
    assert st2 is not None and st2.strike == 105
    assert o.atm_straddle(Chain(EXP, (), ()), 100) is None


def test_straddle_iv_and_moves() -> None:
    half = half_for(0.5, T30)                                    # straddle priced for sigma = 50 %
    assert o.straddle_iv(2 * half, 100, T30) == pytest.approx(0.5)
    assert o.straddle_iv(0, 100, T30) is None and o.straddle_iv(5, 100, 0) is None
    assert o.move_for(100, 0.5, T30) == pytest.approx(100 * 0.5 * math.sqrt(30 / 365))   # 14.33
    assert o.day_move(100, 0.5) == pytest.approx(100 * 0.5 / math.sqrt(252))              # 3.15


def test_quality_grades() -> None:
    assert o.quality(0.05) == "good" and o.quality(0.10) == "good"
    assert o.quality(0.2) == "fair" and o.quality(0.3) == "thin"
    assert o.quality(None) == "none" and o.quality(math.inf) == "none"


def test_realized_vol() -> None:
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * math.exp(0.01 if i % 2 == 0 else -0.01))
    expect = 0.01 * math.sqrt(20 / 19) * math.sqrt(252)          # returns +-1 %, mean 0, sample stdev
    assert o.realized_vol(closes) == pytest.approx(expect)
    assert o.realized_vol(closes[:10]) is None


# ── OI-based numbers ──────────────────────────────────────────────────────


def test_max_pain_by_hand() -> None:
    e = date(2026, 11, 27)
    ch = Chain(e, (c("T", e, "C", 95, 1, 2, oi=100), c("T", e, "C", 100, 1, 2, oi=100)),
               (c("T", e, "P", 100, 1, 2, oi=100), c("T", e, "P", 105, 1, 2, oi=100)))
    # payout at 95: puts 5*100 + 10*100 = 1500; at 100: calls 500 + puts 500 = 1000; at 105: calls 1500
    assert o.max_pain(ch) == 100


def test_oi_unavailable_when_zero_or_missing() -> None:
    assert not o.oi_reported(chain(EXP, 5, oi=None), 100)
    assert not o.oi_reported(chain(EXP, 5, oi=0), 100)
    assert not o.oi_reported(chain(EXP, 5, oi=10), 100)          # reported but tiny (18 x 10 < 500)
    assert o.oi_reported(chain(EXP, 5, oi=100), 100)


def test_walls_and_unusual() -> None:
    e = date(2026, 11, 27)
    ch = Chain(e, (c("T", e, "C", 100, 1, 2, oi=50, vol=10), c("T", e, "C", 110, 1, 2, oi=900, vol=50),
                   c("T", e, "C", 105, 1, 2, oi=100, vol=500)),
               (c("T", e, "P", 95, 1, 2, oi=700, vol=20), c("T", e, "P", 90, 1, 2, oi=300, vol=80)))
    w = o.walls(ch, 101)
    assert w["call_wall"] == {"strike": 110, "oi": 900} and w["put_wall"] == {"strike": 95, "oi": 700}
    u = o.unusual([ch], 101)
    assert [(r["side"], r["strike"], r["vol_oi"]) for r in u] == [("call", 105, 5.0)]   # 500 > 100; others below


def test_skew_put_vs_call_price() -> None:
    ch = chain(EXP, 5.0)
    sk = o.skew(ch, 100, 5)
    # put 95: 5 - 5*0.4 = 3.0 ; call 105: 5 - 5*0.5 = 2.5
    assert sk is not None and (sk["put_strike"], sk["call_strike"]) == (95, 105)
    assert sk["put_call_price_ratio"] == pytest.approx(1.2)
    # call 110 would be 0.05 +- 0.1: no bid -> the nearest quoted call (105) is used
    assert o.skew(ch, 100, 10)["call_strike"] == 105  # type: ignore[index]
    assert o.skew(ch, 100, 0) is None


# ── summary ───────────────────────────────────────────────────────────────


def snap(chains: tuple[Chain, ...], state: str = "REGULAR", price: float = 100.0) -> Snapshot:
    return Snapshot("TEST", price, NOW - 60, state, None, None, chains, NOW)


def test_summary_bands_term_and_warnings() -> None:
    e1, e2, e3 = date(2026, 11, 20), date(2026, 11, 25), EXP     # 2 / 7 / 30 days from NOW (EST)
    t1, t2 = o.years_to_expiry(e1, NOW), o.years_to_expiry(e2, NOW)
    chs = (chain(e1, half_for(0.8, t1)), chain(e2, half_for(0.6, t2)), chain(e3, half_for(0.5, T30), oi=100, vol=0))
    s = o.summarize(snap(chs), NOW, daily_closes=[100 * math.exp(0.01 * (-1) ** i) for i in range(21)])
    assert [round(x.iv or 0, 4) for x in s.expiries] == [0.8, 0.6, 0.5]
    assert s.day is not None and s.day["expiry"] == e1.isoformat()
    assert s.day["move"] == pytest.approx(o.day_move(100, 0.8), abs=1e-4)
    assert s.week is not None and s.week["expiry"] == e2.isoformat()
    assert s.week["up"] == pytest.approx(100 + o.move_for(100, 0.6, t2), abs=1e-4)
    assert s.month is not None and s.month["expiry"] == EXP.isoformat() and s.month["move_pct"] == pytest.approx(14.33, abs=0.01)
    assert s.term == "inverted"                                  # front 80 % > later 50 %
    assert s.session_live and "last_session" not in s.warnings
    assert s.realized_20d is not None and s.iv_vs_realized == pytest.approx(0.5 / s.realized_20d, abs=0.01)
    assert not s.oi_reported and "oi_unverified" in s.warnings and s.walls is None and s.max_pain is None
    assert s.unusual == [] and s.expiries[2].call_oi is None          # OI off by default (unverified), even if sent
    d = o.to_dict(s)
    assert d["expiries"][0]["straddle"]["spread_pct"] == pytest.approx(0.2 * 2 / (2 * half_for(0.8, t1)) * 100, abs=0.1)


def test_summary_last_session_thin_chain_and_expired() -> None:
    e0 = date(2026, 11, 13)                                      # already expired at NOW -> ignored
    wide = chain(EXP, half_for(0.5, T30), spread=4.0)            # spread 8 / straddle 4.6 -> thin
    s = o.summarize(snap((chain(e0, 5), wide), state="POSTPOST"), NOW)
    assert [x.expiry for x in s.expiries] == [EXP.isoformat()]
    assert s.expiries[0].quality == "thin" and s.day is None and s.month is None
    assert "last_session" in s.warnings and "thin_chain" in s.warnings and not s.session_live


def test_summary_oi_walls_max_pain_and_earnings() -> None:
    e2 = date(2026, 11, 25)
    t2 = o.years_to_expiry(e2, NOW)
    s = o.summarize(snap((chain(e2, half_for(0.6, t2), oi=100, vol=10), chain(EXP, half_for(0.5, T30), oi=100))),
                    NOW, earnings_date=date(2026, 12, 1), use_oi=True)
    assert s.oi_reported and s.pc_oi == pytest.approx(1.0) and s.walls is not None and s.max_pain is not None
    assert s.pc_volume == pytest.approx(1.0)
    assert s.most_traded and s.most_traded[0]["volume"] == 10
    s2 = o.summarize(snap((chain(e2, half_for(0.6, t2), oi=None, vol=10),)), NOW, use_oi=True)
    assert "oi_unavailable" in s2.warnings and s2.walls is None
    assert s.earnings is not None and s.earnings["expiry"] == EXP.isoformat()
    assert s.earnings["move_pct"] == pytest.approx(2 * half_for(0.5, T30), abs=0.01)


# ── fetcher normalization + cache ─────────────────────────────────────────


def test_rows_normalized() -> None:
    df = pd.DataFrame([
        {"contractSymbol": "SOXS261127C00083000", "lastTradeDate": pd.Timestamp("2026-11-20 20:59:59", tz="UTC"),
         "strike": 83.0, "lastPrice": 13.35, "bid": 12.05, "ask": 14.6, "volume": 90.0, "openInterest": float("nan"),
         "impliedVolatility": 2.9, "contractSize": "REGULAR", "currency": "USD"},
        {"contractSymbol": "SOXS261127P00083000", "lastTradeDate": pd.NaT, "strike": 83.0, "lastPrice": 0.0,
         "bid": 0.0, "ask": 14.15, "volume": float("nan"), "openInterest": 0, "impliedVolatility": 0.0},
    ])
    a, b = contracts_from_rows(df.to_dict("records"))
    assert a.oi is None and a.iv == 2.9 and a.last_trade == datetime(2026, 11, 20, 20, 59, 59, tzinfo=UTC).timestamp()
    assert a.mid == pytest.approx(13.325)
    assert b.bid is None and b.mid is None and b.last is None and b.volume is None and b.oi == 0 and b.iv is None
    assert b.last_trade is None


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> float:
        return self.t


@pytest.mark.anyio
async def test_cache_ttl_backoff_and_stale() -> None:
    clock, calls = Clock(), []
    fail = {"on": False}

    def fetch(sym: str, n: int) -> Snapshot:
        calls.append((sym, n))
        if fail["on"]:
            raise ConnectionError("down")
        return snap((chain(EXP, 5),), state="REGULAR")

    svc = OptionsService(fetch, max_expiries=4, ttl_live=300, ttl_closed=1800, clock=clock)
    r = await svc.get("test")
    assert r.snapshot is not None and not r.stale and calls == [("TEST", 4)]
    clock.t += 200
    await svc.get("TEST")
    assert len(calls) == 1                                       # within 5 min while live: cached
    clock.t += 200
    fail["on"] = True
    r = await svc.get("TEST")                                    # refresh fails: old data, marked stale
    assert len(calls) == 2 and r.snapshot is not None and r.stale and r.error == "ConnectionError"
    clock.t += 1
    await svc.get("TEST")
    assert len(calls) == 2                                       # backing off (2 s)
    clock.t += 2
    fail["on"] = False
    r = await svc.get("TEST")
    assert len(calls) == 3 and not r.stale and r.error is None


@pytest.mark.anyio
async def test_cache_longer_when_closed_and_symbol_cap() -> None:
    clock, calls = Clock(), []

    def fetch(sym: str, n: int) -> Snapshot:
        calls.append(sym)
        return snap((chain(EXP, 5),), state="CLOSED")

    svc = OptionsService(fetch, clock=clock, max_symbols=2)
    await svc.get("A")
    clock.t += 1000
    await svc.get("A")
    assert calls == ["A"]                                        # closed: 30 min
    await svc.get("B")
    await svc.get("C")                                           # cap 2: A dropped
    await svc.get("A")
    assert calls == ["A", "B", "C", "A"]
    assert timedelta(seconds=svc.ttl_closed) == timedelta(minutes=30)
