"""Event model + pure parsers / calculators (no network). Times are aware datetimes; the screen shows KST.

Sources (ROADMAP Data sources, live check 2026-11-21):
- BLS bls.ics: `DTSTART;TZID=US-Eastern:YYYYMMDDTHHMMSS` + SUMMARY (US Eastern = America/New_York, DST-aware)
- FRED release dates: date only
- FOMC: fomc.yaml (copied from the Fed page), decision = the meeting's last day, date only
- Monthly options expiry: third Friday 16:00 ET (Thursday when that Friday is a market holiday);
  March / June / September / December = quarterly ("quad witching")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

from ruamel.yaml import YAML

from app.timeutil import KST, NY

FOMC_FILE = Path(__file__).with_name("fomc.yaml")
KIND_ORDER = {"market": 0, "macro": 1, "fomc": 2, "user": 3, "earnings": 4, "opex": 5}


@dataclass(frozen=True)
class Event:
    id: str
    kind: str                      # macro | fomc | earnings | market | opex | user
    title: str                     # English
    title_ko: str
    day: date                      # US Eastern date of the event
    at: datetime | None            # exact time (aware) when published; None = date only
    source: str
    importance: str = "medium"     # high | medium | low
    symbol: str | None = None
    note: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def sort_key(self) -> tuple[date, datetime, int, str]:
        # date-only events sort at the start of their US day; same moment: market, macro, FOMC, ... first
        t = self.at or datetime.combine(self.day, time(0, 0), tzinfo=NY)
        return (self.day, t, KIND_ORDER.get(self.kind, 9), self.id)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "title_ko": self.title_ko,
                "date": self.day.isoformat(), "time_known": self.at is not None,
                "time_kst": None if self.at is None else self.at.astimezone(KST).isoformat(),
                "source": self.source, "importance": self.importance, "symbol": self.symbol, "note": self.note,
                "tags": list(self.tags)}


# ── BLS (bls.ics) ─────────────────────────────────────────────────────────

ICS_TZ = {"US-Eastern": NY}
VEVENT = re.compile(r"BEGIN:VEVENT(.*?)END:VEVENT", re.S)
DTSTART = re.compile(r"^DTSTART;TZID=([^:\r\n]+):(\d{8})T(\d{6})", re.M)
SUMMARY = re.compile(r"^SUMMARY:(.*)$", re.M)

# release name in bls.ics -> (short title en, ko, importance)
BLS_RELEASES: dict[str, tuple[str, str, str]] = {
    "Consumer Price Index": ("US CPI", "미국 CPI (소비자물가)", "high"),
    "Employment Situation": ("US jobs report (nonfarm payrolls)", "미국 고용보고서 (비농업 고용)", "high"),
    "Producer Price Index": ("US PPI", "미국 PPI (생산자물가)", "medium"),
    "Job Openings and Labor Turnover Survey": ("US JOLTS job openings", "미국 JOLTS 구인", "medium"),
    "Employment Cost Index": ("US employment cost index", "미국 고용비용지수", "medium"),
}


def _unfold(text: str) -> str:
    """iCalendar line folding: a line starting with a space continues the previous one."""
    return re.sub(r"\r?\n[ \t]", "", text)


def parse_bls_ics(text: str, wanted: Iterable[str] = BLS_RELEASES) -> list[Event]:
    want = set(wanted)
    out = []
    for block in VEVENT.findall(_unfold(text)):
        m, s = DTSTART.search(block), SUMMARY.search(block)
        if not m or not s:
            continue
        name = s.group(1).strip().replace("\\,", ",")
        if name not in want:
            continue
        tz = ICS_TZ.get(m.group(1))
        if tz is None:
            continue
        d = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S").replace(tzinfo=tz)
        en, ko, imp = BLS_RELEASES.get(name, (name, name, "medium"))
        out.append(Event(f"bls:{name}:{d:%Y%m%d}", "macro", en, ko, d.date(), d, "BLS", imp, note=name))
    return out


# ── FRED release dates (date only) ────────────────────────────────────────

FRED_RELEASES: dict[int, tuple[str, str, str]] = {
    53: ("US GDP", "미국 GDP", "high"),
    54: ("US PCE inflation (personal income and outlays)", "미국 PCE 물가 (개인소득·지출)", "high"),
}


def parse_fred_dates(raw: Any, release_id: int, after: date) -> list[Event]:
    en, ko, imp = FRED_RELEASES[release_id]
    out = []
    for x in (raw or {}).get("release_dates", []) if isinstance(raw, dict) else []:
        try:
            d = date.fromisoformat(str(x.get("date")))
        except ValueError:
            continue
        if d >= after:
            out.append(Event(f"fred:{release_id}:{d:%Y%m%d}", "macro", en, ko, d, None, "FRED (BEA)", imp,
                             note="release date only (time not published by FRED)"))
    return out


# ── FOMC ──────────────────────────────────────────────────────────────────


def load_fomc(path: Path = FOMC_FILE) -> list[Event]:
    doc = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    out = []
    for m in doc.get("meetings", []):
        end = m["end"] if isinstance(m["end"], date) else date.fromisoformat(str(m["end"]))
        sep = bool(m.get("sep"))
        en = "FOMC rate decision" + (" + projections" if sep else "")
        ko = "FOMC 금리 결정" + (" + 경제전망" if sep else "")
        note = "; ".join(x for x in (m.get("note"), "date only (the Fed page lists no time); tentative until confirmed")
                         if x)
        out.append(Event(f"fomc:{end:%Y%m%d}", "fomc", en, ko, end, None, "Federal Reserve", "high", note=note,
                         tags=("sep",) if sep else ()))
    return out


# ── monthly options expiry ────────────────────────────────────────────────


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)                       # the third Friday falls on the 15th-21st
    return d + timedelta(days=(4 - d.weekday()) % 7)


def opex_events(start: date, end: date, closed: set[date]) -> list[Event]:
    """Monthly equity options expiry between start and end. A Friday market holiday moves it to Thursday."""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        d = third_friday(y, m)
        if d in closed:
            d -= timedelta(days=1)
        if start <= d <= end:
            quad = m in (3, 6, 9, 12)
            at = datetime.combine(d, time(16, 0), tzinfo=NY)
            out.append(Event(f"opex:{d:%Y%m%d}", "opex",
                             "Quarterly options expiry (quad witching)" if quad else "Monthly options expiry",
                             "분기 옵션 만기 (쿼드러플 위칭)" if quad else "월간 옵션 만기", d, at, "calculated",
                             "high" if quad else "medium", note="third Friday, 16:00 ET"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ── earnings ──────────────────────────────────────────────────────────────

HOURS = {"bmo": ("before the open", "장 시작 전"), "amc": ("after the close", "장 마감 후"),
         "dmh": ("during market hours", "장중")}


def earnings_event(symbol: str, day: date, hour: str | None, importance: str, why: str) -> Event:
    when_en, when_ko = HOURS.get((hour or "").lower(), ("time not announced", "시간 미정"))
    return Event(f"earn:{symbol}:{day:%Y%m%d}", "earnings", f"{symbol} earnings ({when_en})",
                 f"{symbol} 실적 발표 ({when_ko})", day, None, "Finnhub", importance, symbol=symbol,
                 note=why, tags=((hour or "").lower(),) if hour else ())


# ── merge ─────────────────────────────────────────────────────────────────


def merge(events: Iterable[Event], start: datetime, end: datetime) -> list[Event]:
    """Upcoming events in [start, end), de-duplicated by id, sorted. A date-only event counts until its US day ends."""
    seen: dict[str, Event] = {}
    for e in events:
        if e.at is not None:
            keep = start <= e.at < end
        else:
            day_end = datetime.combine(e.day + timedelta(days=1), time(0, 0), tzinfo=NY)
            day_start = datetime.combine(e.day, time(0, 0), tzinfo=NY)
            keep = day_end > start and day_start < end
        if keep:
            seen.setdefault(e.id, e)
    return sorted(seen.values(), key=Event.sort_key)
