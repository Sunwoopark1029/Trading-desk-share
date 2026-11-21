"""Code-calculated levels for the AI (get_levels tool, context and live blocks).

Every value comes from the indicator code / positions.json / Toss data the dashboard already
has. Each level has a stable id the AI must cite (e.g. "ema20_5m", "fib_0.618", "swing_high_3").
Prices are Decimal; the currency is the ticker's (USD for US stocks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services import KEY_QUOTES, Services
from app.timeutil import epoch_to_kst_iso

RECENT_SWINGS = 6
SHOWN_DP = Decimal("0.0001")


def shown_price(p: Decimal) -> str:
    """Price as given to the AI: at most 4 decimals (indicator values can have 25+)."""
    q = p.quantize(SHOWN_DP) if p.as_tuple().exponent < -4 else p  # type: ignore[operator]
    return format(q, "f")


@dataclass(frozen=True)
class Level:
    id: str
    label: str
    price: Decimal
    kind: str                 # last | ema | vwap | fib | swing | session | prev | avg | plan | user_fib | ai_line
    time: str | None = None   # KST ISO when the level belongs to a bar

    def as_dict(self, currency: str, last: Decimal | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "label": self.label, "price": shown_price(self.price),
                             "currency": currency, "kind": self.kind}
        if last is not None and self.id != "last" and last != 0:
            d["vs_last"] = vs_last(self.price, last)
            diff = self.price - last
            d["diff_from_last"] = shown_price(diff)                      # level - last, USD
            d["diff_from_last_pct"] = format((diff / last * 100).quantize(Decimal("0.01")), "f")
        if self.time:
            d["time"] = self.time
        return d


@dataclass
class LevelSet:
    symbol: str
    timeframe: str
    currency: str
    fixed: list[Level] = field(default_factory=list)   # change at most once a day (cached context)
    live: list[Level] = field(default_factory=list)    # change every bar (live block)
    notes: list[str] = field(default_factory=list)
    trading_date: str | None = None
    updated: str | None = None
    source: str = "Toss (real-time) + dashboard indicator code"

    @property
    def all(self) -> list[Level]:
        return [*self.fixed, *self.live]

    def prices(self) -> list[Decimal]:
        return [lv.price for lv in self.all]

    def as_dict(self, part: str = "all") -> dict[str, Any]:
        items = self.fixed if part == "fixed" else self.live if part == "live" else self.all
        return {"symbol": self.symbol, "timeframe": self.timeframe, "currency": self.currency,
                "trading_date": self.trading_date, "updated_kst": self.updated, "source": self.source,
                "last_price": None if self.last is None else shown_price(self.last),
                "levels": [lv.as_dict(self.currency, self.last) for lv in items], "notes": self.notes}

    @property
    def last(self) -> Decimal | None:
        return next((lv.price for lv in self.live if lv.id == "last"), None)


def _last(values: list[Any] | None) -> Decimal | None:
    for v in reversed(values or []):
        if v is not None:
            return Decimal(str(v))
    return None


def _ratio_id(r: Any) -> str:
    return format(Decimal(str(r)).normalize(), "f")


async def compute_levels(svc: Services, symbol: str, timeframe: str = "5m") -> LevelSet:
    """All levels for symbol on timeframe (1m / 5m / 1d). Never invents a value: anything
    that cannot be computed is left out and named in `notes`."""
    tf = timeframe if timeframe in ("1m", "5m", "1d") else "5m"
    view = await svc.candles_view(symbol, tf)
    cur = view["meta"].get("currency") or "USD"
    ls = LevelSet(symbol, tf, cur, updated=view["meta"].get("updated"))
    bars = view["bars"]
    if not bars:
        ls.notes.append("no bars loaded yet (check Toss status)")

    # live: last price, EMA, VWAP
    q_e = svc.cache.get(KEY_QUOTES)
    quote = (q_e.value or {}).get(symbol) if q_e and q_e.value else None
    if quote is not None:
        ls.live.append(Level("last", "Last price (Toss quote)", quote.last_price, "last",
                             epoch_to_kst_iso(quote.timestamp.timestamp()) if quote.timestamp else None))
    elif bars:
        b = bars[-1]
        ls.live.append(Level("last", f"Last {tf} close", Decimal(str(b["close"])), "last",
                             epoch_to_kst_iso(b["end"])))
    for n, vals in sorted(view["ema"].items(), key=lambda kv: int(kv[0])):
        v = _last(vals)
        if v is not None:
            ls.live.append(Level(f"ema{n}_{tf}", f"EMA {n} ({tf})", v, "ema"))
        else:
            ls.notes.append(f"EMA {n} ({tf}) not available (too few bars)")
    vw = _last(view.get("vwap"))
    if vw is not None:
        label = "Session VWAP" + (f" ({view['vwap_label']})" if view.get("vwap_label") else "")
        ls.live.append(Level(f"vwap_{tf}", label, vw, "vwap"))

    # live: today's session range + default Fibonacci
    fib = view.get("fib")
    if fib:
        ls.trading_date = str(view.get("fib_date"))
        ls.live.append(Level("session_low", f"Session low {view.get('fib_date')} (pre+regular+after)",
                             Decimal(str(fib["low"])), "session"))
        ls.live.append(Level("session_high", f"Session high {view.get('fib_date')} (pre+regular+after)",
                             Decimal(str(fib["high"])), "session"))
        for lv in fib["levels"]:
            rid = _ratio_id(lv["ratio"])
            ls.live.append(Level(f"fib_{rid}", f"Fib {rid} ({fib['direction']}, session low/high)",
                                 Decimal(str(lv["price"])), "fib"))

    # live: recent swings (with bar start time: trendlines must use these points)
    sw = view.get("swings") or []
    highs = [s for s in sw if s["kind"] == "high"][-RECENT_SWINGS:]
    lows = [s for s in sw if s["kind"] == "low"][-RECENT_SWINGS:]
    for kind, items in (("high", highs), ("low", lows)):
        for i, s in enumerate(reversed(items), start=1):  # 1 = most recent
            ls.live.append(Level(f"swing_{kind}_{i}", f"Swing {kind} #{i} ({tf}, 1 = most recent)",
                                 Decimal(str(s["price"])), "swing", epoch_to_kst_iso(s["start"])))

    # fixed: previous day (daily bars are regular session only)
    if tf != "1d":
        try:
            daily = await svc.candles_view(symbol, "1d")
            dbars = daily["bars"]
            if ls.trading_date:
                dbars = [b for b in dbars if b.get("date", "") < ls.trading_date]
            if dbars:
                p = dbars[-1]
                d = p.get("date")
                ls.fixed.append(Level("prev_close", f"Previous close {d} (regular session)", Decimal(str(p["close"])), "prev"))
                ls.fixed.append(Level("prev_high", f"Previous day high {d} (regular session)", Decimal(str(p["high"])), "prev"))
                ls.fixed.append(Level("prev_low", f"Previous day low {d} (regular session)", Decimal(str(p["low"])), "prev"))
        except Exception:  # noqa: BLE001 - daily data optional
            ls.notes.append("previous-day levels not available")

    # fixed: my info (avg price, plans), saved Fibonacci / AI lines
    try:
        pos = svc.positions_view()
        for o in pos["overlays"]:
            if o.get("symbol") not in (None, symbol):
                continue
            price = Decimal(str(o["points"][0]["price"]))
            if str(o["id"]).startswith("avg-"):
                ls.fixed.append(Level("avg_price", o["label"], price, "avg"))
            else:
                ls.fixed.append(Level(f"plan_{o['id']}", o["label"], price, "plan"))
    except Exception:  # noqa: BLE001 - positions.json problems are shown elsewhere
        ls.notes.append("positions.json not readable: no avg price or plans")
    try:                                                    # saved research thesis (phase 6 part C)
        from app.research.store import research_store_of
        th = research_store_of(svc).current(symbol)
        if th is not None and th.get("currency") == "USD":   # US listings: the levels are USD like the chart
            day = str(th.get("saved_at", ""))[:10]
            for r in th.get("reference_levels", []):
                if r.get("price") is None:
                    continue
                ls.fixed.append(Level(f"thesis_{r['id']}",
                                      f"Research thesis: {r.get('label')} ({r.get('display')}, {r.get('source')}, "
                                      f"thesis saved {day})", Decimal(str(r["price"])), "research"))
    except Exception:  # noqa: BLE001 - the thesis store is optional
        ls.notes.append("research thesis not readable")
    try:
        for o in svc.list_overlays(symbol, tf):            # only lines drawn on this timeframe
            if o["type"] == "fib" and o.get("levels"):
                for lv in o["levels"]:
                    rid = _ratio_id(lv["ratio"])
                    ls.fixed.append(Level(f"{o['source']}fib_{o['id']}_{rid}",
                                          f"{o.get('label') or 'Fib'} {rid} ({o['source']} line {o['id']})",
                                          Decimal(str(lv["price"])), "user_fib"))
            elif o["source"] == "ai":
                for i, pt in enumerate(o["points"]):
                    ls.fixed.append(Level(f"ai_{o['id']}_{i}", f"AI {o['type']} '{o.get('label', '')}' point {i + 1}",
                                          Decimal(str(pt["price"])), "ai_line", pt.get("time")))
    except Exception:  # noqa: BLE001
        ls.notes.append("saved lines not readable")
    return ls


def vs_last(price: Decimal, last: Decimal, rel_tol: Decimal = Decimal("0.0005")) -> str:
    """Where a level sits relative to the last price: "above" (resistance side), "below"
    (support side) or "at" (within 0.05%)."""
    if abs(price - last) <= abs(last) * rel_tol:
        return "at"
    return "above" if price > last else "below"


def match_price(price: Decimal, known: list[Decimal], rel_tol: Decimal = Decimal("0.0005")) -> Decimal | None:
    """The known value closest to price if within rel_tol (0.05% default), else None."""
    hits = [k for k in known if k and abs(price - k) <= abs(k) * rel_tol]
    return min(hits, key=lambda k: abs(price - k)) if hits else None


def price_matches(price: Decimal, known: list[Decimal], rel_tol: Decimal = Decimal("0.0005")) -> bool:
    return match_price(price, known, rel_tol) is not None
