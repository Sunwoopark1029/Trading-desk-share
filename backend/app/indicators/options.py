"""Options-implied context for stock / ETF trades (user idea 2026-11-20; plan step 2, 2026-11-21).

Pure calculations on an option-chain snapshot (fetched by app/data/yf_options.py). Nothing here prices options:
the desk trades no options. Facts from the live check (ROADMAP Data sources, 2026-11-21) shape the rules:

- Yahoo's impliedVolatility is a decimal but unreliable (call and put IV at one strike can differ by 15 points),
  so the main IV is taken from the at-the-money straddle mid-price with the closed-form approximation
  straddle ~= sqrt(2/pi) * S * sigma * sqrt(T)  (Brenner-Subrahmanyam), i.e. sigma = straddle / (0.798 * S * sqrt(T)).
  Yahoo's IV is kept only as a cross-check.
- openInterest is not trustworthy (2026-11-21: SPY at-the-money OI 24-34 contracts, really tens of thousands; many
  values 0 or missing). OI-based numbers (walls, max pain, OI put/call, volume > OI) are therefore UNVERIFIED and off
  (`use_oi=False`); volume looks plausible and drives "most traded strikes" instead.
- Adjusted contracts (non-standard deliverables after a split) come with the normal root (SOXX...C00123330), so they
  are dropped by strike grid: a strike that is not a multiple of 0.50 is adjusted. A root other than the symbol
  (e.g. SOXX1) is dropped too. Limit: an adjusted strike that happens to sit on the 0.50 grid can't be told apart.
- Options expire 16:00 America/New_York on the expiry date.

Expected move = the market's estimate of SIZE, never direction.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Sequence

from app.timeutil import NY, UTC

YEAR_S = 365 * 24 * 3600
TRADING_DAYS = 252                       # one trading day = sigma / sqrt(252) (market convention)
SQRT_2_OVER_PI = math.sqrt(2 / math.pi)  # 0.7979: ATM straddle ~= 0.798 * S * sigma * sqrt(T)
EXPIRY_TIME = time(16, 0)                # US equity options stop trading 16:00 ET on the expiry date

# quality grade of an expiry from the ATM spread (call + put spreads / straddle mid)
GOOD_SPREAD = 0.10
FAIR_SPREAD = 0.25
# OI is "reported" for a chain when at least this share of near-the-money contracts has OI > 0 ...
OI_COVERAGE_MIN = 0.5
# ... and the near-the-money OI adds up to at least this many contracts
OI_TOTAL_MIN = 500
NEAR_PCT = 0.20                          # "near the money" = strikes within +-20 % of the price
UNUSUAL_MIN_VOLUME = 100                 # unusual activity: volume >= this and volume > OI
MIN_DAY_EXPIRY_DAYS = 1.0                # the "day" move uses the first good expiry at least 1 day away


@dataclass(frozen=True)
class Contract:
    contract: str                 # OCC symbol, e.g. SOXS261127C00083000
    strike: float
    bid: float | None
    ask: float | None
    last: float | None
    volume: float | None
    oi: float | None              # None = not reported (Yahoo often sends 0 / NaN)
    iv: float | None              # Yahoo's IV (decimal); cross-check only
    last_trade: float | None      # epoch seconds (UTC)

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Chain:
    expiry: date
    calls: tuple[Contract, ...]
    puts: tuple[Contract, ...]


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    price: float                  # underlying regularMarketPrice from the same response (same time as the quotes)
    price_time: float | None      # epoch seconds
    market_state: str | None      # Yahoo marketState: REGULAR, PRE, POST, POSTPOST, CLOSED, ...
    post_price: float | None
    pre_price: float | None
    chains: tuple[Chain, ...]
    fetched_at: float
    expiries_all: tuple[date, ...] = ()


# ── basics ────────────────────────────────────────────────────────────────


def expiry_epoch(d: date) -> float:
    """Expiry moment: 16:00 America/New_York on the expiry date, as UTC epoch seconds (DST-aware)."""
    return datetime.combine(d, EXPIRY_TIME, tzinfo=NY).astimezone(UTC).timestamp()


def years_to_expiry(d: date, now: float) -> float:
    return max(0.0, (expiry_epoch(d) - now) / YEAR_S)


def standard(c: Contract, symbol: str) -> bool:
    """A standard contract of `symbol` (OCC root == symbol). Adjusted contracts (root SOXX1 etc.) are dropped."""
    m = re.fullmatch(r"([A-Z.]+)(\d{6})([CP])(\d{8})", c.contract or "")
    on_grid = abs(c.strike * 2 - round(c.strike * 2)) < 1e-6
    return bool(m) and m.group(1) == re.sub(r"[.\-]", "", symbol.upper()) and on_grid


def clean(chain: Chain, symbol: str) -> Chain:
    return Chain(chain.expiry,
                 tuple(c for c in chain.calls if standard(c, symbol)),
                 tuple(c for c in chain.puts if standard(c, symbol)))


@dataclass(frozen=True)
class Straddle:
    strike: float
    call_mid: float
    put_mid: float
    mid: float
    spread_pct: float             # (call spread + put spread) / straddle mid
    yahoo_iv: float | None        # mean of Yahoo's call / put IV at this strike (cross-check)


def atm_straddle(chain: Chain, price: float) -> Straddle | None:
    """The straddle at the strike nearest the price where both call and put have a two-sided quote."""
    puts = {p.strike: p for p in chain.puts}
    best: tuple[float, Straddle] | None = None
    for c in chain.calls:
        p = puts.get(c.strike)
        if p is None or c.mid is None or p.mid is None:
            continue
        dist = abs(c.strike - price)
        if best is not None and dist >= best[0]:
            continue
        mid = c.mid + p.mid
        spread = ((c.ask or 0) - (c.bid or 0)) + ((p.ask or 0) - (p.bid or 0))
        ivs = [x for x in (c.iv, p.iv) if x is not None and x > 0]
        best = (dist, Straddle(c.strike, c.mid, p.mid, mid, spread / mid if mid > 0 else math.inf,
                               sum(ivs) / len(ivs) if ivs else None))
    if best is None or best[0] > price * 0.05:      # no two-sided quote within 5 % of the price
        return None
    return best[1]


def straddle_iv(straddle_mid: float, price: float, t_years: float) -> float | None:
    """sigma from the ATM straddle: straddle / (sqrt(2/pi) * S * sqrt(T))."""
    if straddle_mid <= 0 or price <= 0 or t_years <= 0:
        return None
    return straddle_mid / (SQRT_2_OVER_PI * price * math.sqrt(t_years))


def quality(spread_pct: float | None) -> str:
    if spread_pct is None or not math.isfinite(spread_pct):
        return "none"
    return "good" if spread_pct <= GOOD_SPREAD else "fair" if spread_pct <= FAIR_SPREAD else "thin"


def move_for(price: float, iv: float, t_years: float) -> float:
    """1-sigma move over t_years: S * sigma * sqrt(T)."""
    return price * iv * math.sqrt(max(t_years, 0.0))


def day_move(price: float, iv: float) -> float:
    """1-sigma move over one trading day: S * sigma / sqrt(252)."""
    return price * iv / math.sqrt(TRADING_DAYS)


def realized_vol(closes: Sequence[float], days: int = 20) -> float | None:
    """Annualized close-to-close volatility of the last `days` daily log returns (sample stdev * sqrt(252))."""
    xs = [float(x) for x in closes if x is not None and float(x) > 0]
    if len(xs) < days + 1:
        return None
    xs = xs[-(days + 1):]
    rets = [math.log(b / a) for a, b in zip(xs, xs[1:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


# ── open interest, positioning ────────────────────────────────────────────


def near(contracts: Sequence[Contract], price: float, pct: float = NEAR_PCT) -> list[Contract]:
    return [c for c in contracts if abs(c.strike - price) <= price * pct]


def oi_reported(chain: Chain, price: float) -> bool:
    """Enough OI is reported near the money to use OI-based numbers."""
    cs = near(chain.calls, price) + near(chain.puts, price)
    if not cs:
        return False
    with_oi = [c for c in cs if c.oi is not None and c.oi > 0]
    return len(with_oi) / len(cs) >= OI_COVERAGE_MIN and sum(c.oi or 0 for c in with_oi) >= OI_TOTAL_MIN


def max_pain(chain: Chain) -> float | None:
    """Strike where option holders' total intrinsic value at expiry is smallest (a theory, not a forecast)."""
    strikes = sorted({c.strike for c in chain.calls} | {p.strike for p in chain.puts})
    if not strikes:
        return None
    best: tuple[float, float] | None = None
    for k in strikes:
        pay = sum((k - c.strike) * (c.oi or 0) for c in chain.calls if k > c.strike)
        pay += sum((p.strike - k) * (p.oi or 0) for p in chain.puts if k < p.strike)
        if best is None or pay < best[0]:
            best = (pay, k)
    return None if best is None else best[1]


def walls(chain: Chain, price: float) -> dict[str, Any]:
    """Largest call OI at / above the price and largest put OI at / below it (near the money)."""
    calls = [c for c in near(chain.calls, price) if c.strike >= price and (c.oi or 0) > 0]
    puts = [p for p in near(chain.puts, price) if p.strike <= price and (p.oi or 0) > 0]
    cw = max(calls, key=lambda c: c.oi or 0, default=None)
    pw = max(puts, key=lambda p: p.oi or 0, default=None)
    return {"call_wall": None if cw is None else {"strike": cw.strike, "oi": cw.oi},
            "put_wall": None if pw is None else {"strike": pw.strike, "oi": pw.oi}}


def ratio(a: float, b: float) -> float | None:
    return None if b <= 0 else a / b


def unusual(chains: Sequence[Chain], price: float, top: int = 5) -> list[dict[str, Any]]:
    """Contracts trading today above their open interest (new positions), largest volume first."""
    rows = []
    for ch in chains:
        for side, cs in (("call", ch.calls), ("put", ch.puts)):
            for c in near(cs, price):
                if c.volume is None or c.volume < UNUSUAL_MIN_VOLUME or c.oi is None or c.oi <= 0:
                    continue
                if c.volume > c.oi:
                    rows.append({"expiry": ch.expiry.isoformat(), "side": side, "strike": c.strike,
                                 "volume": c.volume, "oi": c.oi, "vol_oi": round(c.volume / c.oi, 1)})
    rows.sort(key=lambda r: -r["volume"])
    return rows[:top]


def most_traded(chains: Sequence[Chain], price: float, top: int = 5) -> list[dict[str, Any]]:
    """Strikes with the most volume today (near the money, first chains), largest first. Volume only (OI unverified)."""
    rows = [{"expiry": ch.expiry.isoformat(), "side": side, "strike": c.strike, "volume": c.volume}
            for ch in chains for side, cs in (("call", ch.calls), ("put", ch.puts))
            for c in near(cs, price) if c.volume]
    rows.sort(key=lambda r: -(r["volume"] or 0))
    return rows[:top]


def nearest_mid(contracts: Sequence[Contract], strike: float) -> tuple[float, float] | None:
    """(strike, mid) of the contract with a two-sided quote nearest `strike`."""
    q = [c for c in contracts if c.mid is not None]
    if not q:
        return None
    c = min(q, key=lambda c: abs(c.strike - strike))
    return c.strike, c.mid  # type: ignore[return-value]


def skew(chain: Chain, price: float, move: float) -> dict[str, Any] | None:
    """Put vs call price at about one expected move away: put mid (strike ~ S - move) / call mid (strike ~ S + move).
    > 1 = downside protection costs more than upside calls. Model-free (no IV needed)."""
    if move <= 0:
        return None
    p = nearest_mid(chain.puts, price - move)
    c = nearest_mid(chain.calls, price + move)
    if p is None or c is None or c[1] <= 0:
        return None
    # both strikes must really be out of the money and roughly one move away
    if p[0] >= price or c[0] <= price:
        return None
    return {"put_strike": p[0], "put_mid": round(p[1], 4), "call_strike": c[0], "call_mid": round(c[1], 4),
            "put_call_price_ratio": round(p[1] / c[1], 2)}


# ── the summary ───────────────────────────────────────────────────────────


@dataclass
class ExpiryStats:
    expiry: str
    days: float
    straddle: Straddle | None
    iv: float | None              # straddle-implied (main)
    quality: str
    move: float | None            # 1-sigma move to expiry (S * iv * sqrt(T))
    call_volume: float
    put_volume: float
    call_oi: float | None
    put_oi: float | None


@dataclass
class OptionsSummary:
    symbol: str
    price: float
    price_time: float | None
    market_state: str | None
    session_live: bool            # quotes from a live regular session (else: last session's quotes)
    fetched_at: float
    expiries: list[ExpiryStats]
    day: dict[str, Any] | None = None           # one-trading-day 1-sigma move and bands
    week: dict[str, Any] | None = None          # to the first expiry >= 4 days away
    month: dict[str, Any] | None = None         # to the first expiry >= 20 days away
    realized_20d: float | None = None
    iv_vs_realized: float | None = None
    term: str | None = None                     # "inverted" when the front IV is above the later one
    skew: dict[str, Any] | None = None
    pc_volume: float | None = None
    pc_oi: float | None = None
    oi_reported: bool = False
    walls: dict[str, Any] | None = None
    max_pain: dict[str, Any] | None = None
    unusual: list[dict[str, Any]] = field(default_factory=list)
    most_traded: list[dict[str, Any]] = field(default_factory=list)
    earnings: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


def _band(price: float, move: float, iv: float, expiry: str | None, quality_: str) -> dict[str, Any]:
    return {"move": round(move, 4), "up": round(price + move, 4), "down": round(price - move, 4),
            "move_pct": round(move / price * 100, 2), "iv": round(iv, 4), "expiry": expiry, "quality": quality_}


def summarize(snap: Snapshot, now: float, daily_closes: Sequence[float] | None = None,
              earnings_date: date | None = None, use_oi: bool = False) -> OptionsSummary:
    """Everything the desk shows and the AI reads, from one snapshot. All numbers are calculated here."""
    price = snap.price
    chains = [clean(ch, snap.symbol) for ch in snap.chains]
    chains = [ch for ch in chains if expiry_epoch(ch.expiry) > now]
    stats: list[ExpiryStats] = []
    for ch in chains:
        t = years_to_expiry(ch.expiry, now)
        st = atm_straddle(ch, price)
        iv = straddle_iv(st.mid, price, t) if st else None
        rep = use_oi and oi_reported(ch, price)
        stats.append(ExpiryStats(
            expiry=ch.expiry.isoformat(), days=round(t * 365, 2), straddle=st, iv=iv,
            quality=quality(st.spread_pct if st else None),
            move=move_for(price, iv, t) if iv else None,
            call_volume=sum(c.volume or 0 for c in ch.calls), put_volume=sum(p.volume or 0 for p in ch.puts),
            call_oi=sum(c.oi or 0 for c in ch.calls) if rep else None,
            put_oi=sum(p.oi or 0 for p in ch.puts) if rep else None))

    live = (snap.market_state or "").upper() == "REGULAR"
    out = OptionsSummary(snap.symbol, price, snap.price_time, snap.market_state, live, snap.fetched_at, stats)
    if not live:
        out.warnings.append("last_session")
    usable = [s for s in stats if s.iv is not None and s.quality in ("good", "fair")]
    if not usable:
        out.warnings.append("thin_chain" if stats else "no_chain")

    def first(min_days: float) -> ExpiryStats | None:
        return next((s for s in usable if s.days >= min_days), None)

    d = first(MIN_DAY_EXPIRY_DAYS)
    if d and d.iv:
        out.day = _band(price, day_move(price, d.iv), d.iv, d.expiry, d.quality)
    for attr, min_days in (("week", 4.0), ("month", 20.0)):
        s = first(min_days)
        if s and s.iv and s.move:
            setattr(out, attr, _band(price, s.move, s.iv, s.expiry, s.quality) | {"days": s.days})

    if daily_closes:
        rv = realized_vol(daily_closes)
        out.realized_20d = None if rv is None else round(rv, 4)
        front = out.month or out.week or out.day
        if rv and front:
            out.iv_vs_realized = round(front["iv"] / rv, 2)

    if len(usable) >= 2:
        a, b = usable[0], usable[min(len(usable) - 1, 2)]
        if a.iv and b.iv:
            out.term = "inverted" if a.iv > b.iv * 1.05 else "normal" if b.iv > a.iv * 1.05 else "flat"

    ref = first(4.0) or (usable[0] if usable else None)
    if ref and ref.move:
        ch = next(c for c in chains if c.expiry.isoformat() == ref.expiry)
        sk = skew(ch, price, ref.move)
        out.skew = None if sk is None else sk | {"expiry": ref.expiry}

    near_chains = chains[:3]
    cv = sum(s.call_volume for s in stats[:3])
    pv = sum(s.put_volume for s in stats[:3])
    out.pc_volume = None if ratio(pv, cv) is None else round(pv / cv, 2)
    if not use_oi:
        out.warnings.append("oi_unverified")
    elif chains and oi_reported(chains[0], price):
        out.oi_reported = True
        coi = sum(s.call_oi or 0 for s in stats[:3] if s.call_oi is not None)
        poi = sum(s.put_oi or 0 for s in stats[:3] if s.put_oi is not None)
        out.pc_oi = None if ratio(poi, coi) is None else round(poi / coi, 2)
        out.walls = walls(chains[0], price) | {"expiry": chains[0].expiry.isoformat()}
        mp = max_pain(chains[0])
        out.max_pain = None if mp is None else {"strike": mp, "expiry": chains[0].expiry.isoformat()}
        out.unusual = unusual(near_chains, price)
    else:
        out.warnings.append("oi_unavailable")
    out.most_traded = most_traded(near_chains, price)

    if earnings_date is not None:
        e = next((s for s in usable if date.fromisoformat(s.expiry) >= earnings_date), None)
        if e and e.straddle:
            out.earnings = {"date": earnings_date.isoformat(), "expiry": e.expiry, "straddle": round(e.straddle.mid, 4),
                            "move_pct": round(e.straddle.mid / price * 100, 2), "quality": e.quality,
                            "note": "straddle of the first expiry after earnings; includes the normal days up to it"}
    return out


def to_dict(s: OptionsSummary) -> dict[str, Any]:
    """JSON-ready view (rounded); KST conversion happens at the route."""
    def ex(e: ExpiryStats) -> dict[str, Any]:
        st = e.straddle
        return {"expiry": e.expiry, "days": e.days, "quality": e.quality,
                "iv": None if e.iv is None else round(e.iv, 4),
                "move": None if e.move is None else round(e.move, 4),
                "straddle": None if st is None else {
                    "strike": st.strike, "mid": round(st.mid, 4), "spread_pct": round(st.spread_pct * 100, 1),
                    "yahoo_iv": None if st.yahoo_iv is None else round(st.yahoo_iv, 4)},
                "call_volume": e.call_volume, "put_volume": e.put_volume, "call_oi": e.call_oi, "put_oi": e.put_oi}
    return {
        "symbol": s.symbol, "price": s.price, "price_time": s.price_time, "market_state": s.market_state,
        "session_live": s.session_live, "fetched_at": s.fetched_at,
        "day": s.day, "week": s.week, "month": s.month,
        "realized_20d": s.realized_20d, "iv_vs_realized": s.iv_vs_realized, "term": s.term, "skew": s.skew,
        "pc_volume": s.pc_volume, "pc_oi": s.pc_oi, "oi_reported": s.oi_reported, "walls": s.walls,
        "max_pain": s.max_pain, "unusual": s.unusual, "most_traded": s.most_traded, "earnings": s.earnings, "warnings": s.warnings,
        "expiries": [ex(e) for e in s.expiries],
    }
