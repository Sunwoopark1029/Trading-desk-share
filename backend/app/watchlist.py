"""Watchlist folders (phase 6 part A): data/watchlist.json next to positions.json, gitignored, atomic write.

Document: {"version": 1, "folders": [{"id", "name", "pinned", "symbols": [...]}], "updated": KST ISO}
- 1-20 folders, up to 100 tickers each, names unique (case-insensitive), ids unique, exactly one pinned.
- Every ticker must be in the Toss US stock list (same check as the rest of the app).
- Invalid JSON in the file is never overwritten: reads report it, saves are refused until it is fixed by hand.
- First run (no file): a pinned "Favorites" folder from the default ticker + the Settings watchlist.
The pinned folder will supply the chart's quick buttons and the top strip's stock slots (step 4).
Quotes: Toss /prices for one folder, change vs the previous business day's close (1d bar, cached per day);
change and change % are calculated here, never by the AI.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app import store
from app.services import SYMBOL_RE, InputError, Services, epoch_kst, error_info

log = logging.getLogger("desk.watchlist")
MAX_FOLDERS = 20
MAX_PER_FOLDER = 100
MAX_NAME = 40
PREV_CLOSE_PER_REQUEST = 20        # missing previous closes fetched per quotes request (rest: next request)
PREV_CLOSE_RETRY_SECONDS = 60      # a failed previous-close lookup waits this long before the next try
FOLDER_ID_RE = re.compile(r"^[a-z0-9-]{1,24}\Z")
DEFAULT_NAME = "Favorites"
_lock = threading.Lock()


class WatchlistError(Exception):
    """The file itself is broken (message is safe to show). Saving is refused so it is never overwritten."""


def path_for(svc: Services) -> Path:
    return Path(svc.positions_path).parent / "watchlist.json"


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "folder"
    cand, n = base, 2
    while cand in taken:
        cand, n = f"{base[:20]}-{n}", n + 1
    return cand


def validate(doc: Any, check_symbol: Any) -> dict[str, Any]:
    """Whole document from the screen -> normalized document, or InputError (message safe to show)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("folders"), list):
        raise InputError("watchlist must be an object with a folders list")
    raw = doc["folders"]
    if not 1 <= len(raw) <= MAX_FOLDERS:
        raise InputError(f"keep 1 to {MAX_FOLDERS} folders")
    names: set[str] = set()
    ids: set[str] = set()
    folders = []
    for f in raw:
        if not isinstance(f, dict):
            raise InputError("each folder must be an object")
        name = f.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
            raise InputError(f"folder names must be 1-{MAX_NAME} characters")
        name = " ".join(name.split())
        if name.lower() in names:
            raise InputError(f"two folders are called '{name}'")
        names.add(name.lower())
        fid = f.get("id")
        if fid is not None and (not isinstance(fid, str) or not FOLDER_ID_RE.match(fid)):
            raise InputError("folder id must be lower-case letters, digits or '-' (1-24)")
        syms = f.get("symbols", [])
        if not isinstance(syms, list):
            raise InputError(f"'{name}': symbols must be a list")
        clean: list[str] = []
        for s in syms:
            if not isinstance(s, str):
                raise InputError(f"'{name}': tickers must be text")
            sym = check_symbol(s.strip())          # Toss US list; raises InputError
            if sym not in clean:
                clean.append(sym)
        if len(clean) > MAX_PER_FOLDER:
            raise InputError(f"'{name}': at most {MAX_PER_FOLDER} tickers per folder")
        folders.append({"id": fid, "name": name, "pinned": f.get("pinned") is True, "symbols": clean})
    for f in folders:                               # existing ids keep their id (first one wins on a duplicate)
        if f["id"] is not None and f["id"] not in ids:
            ids.add(f["id"])
        else:
            f["id"] = None
    for f in folders:                               # then new folders get a free id from their name
        if f["id"] is None:
            f["id"] = _slug(f["name"], ids)
            ids.add(f["id"])
    pinned = [f for f in folders if f["pinned"]]
    if len(pinned) > 1:
        raise InputError("only one folder can be pinned")
    if not pinned:
        folders[0]["pinned"] = True                 # always one pinned folder (it feeds the quick buttons)
    return {"version": 1, "folders": folders}


class Watchlist:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.path = path_for(svc)

    def _check(self, raw: str) -> str:
        return self.svc.validate_symbol(raw)

    def migrated(self) -> dict[str, Any]:
        """First-run document: default ticker + Settings watchlist (today's quick buttons) in one pinned folder."""
        t = self.svc.config.tickers
        syms = list(dict.fromkeys(s.upper() for s in [t.default, *t.compare, *self.svc.config.news.extra]))
        return {"version": 1, "folders": [{"id": "favorites", "name": DEFAULT_NAME, "pinned": True,
                                           "symbols": syms[:MAX_PER_FOLDER]}]}

    def load(self) -> dict[str, Any]:
        """Saved document, or the first-run one (written once). Broken file -> WatchlistError."""
        with _lock:
            try:
                raw = store.read_json(self.path)
            except store.StoreError as exc:
                raise WatchlistError(str(exc)) from None
            if raw is None:
                doc = self.migrated()
                doc["updated"] = epoch_kst(self.svc.clock())
                store.atomic_write_json(self.path, doc)
                log.info("watchlist created from the default ticker + Settings watchlist")
                return doc
            if not isinstance(raw, dict) or not isinstance(raw.get("folders"), list):
                raise WatchlistError(f"{self.path.name} has an unexpected shape. Fix it by hand; "
                                     "the file was not changed.")
            try:   # same rules as a save (format only for tickers: they were checked when saved)
                clean = validate(raw, _format_only)
            except InputError as exc:
                raise WatchlistError(f"{self.path.name}: {exc}. Fix it by hand; the file was not changed.") from None
            clean["updated"] = raw.get("updated")
            return clean

    def save(self, doc: Any) -> dict[str, Any]:
        """Replace the whole document (validated). Refused while the file on disk is broken."""
        saved = self.load()                          # raises WatchlistError if the file is broken
        known = {s for f in saved["folders"] for s in f["symbols"]}

        def check(raw: str) -> str:                  # tickers already saved are trusted (format only), so a
            sym = _format_only(raw)                  # rename works while the Toss list is still loading;
            return sym if sym in known else self._check(raw)   # new tickers must be in the Toss list

        clean = validate(doc, check)
        clean["updated"] = epoch_kst(self.svc.clock())
        with _lock:
            store.atomic_write_json(self.path, clean)
        return clean

    def pinned_symbols(self) -> list[str]:
        """Pinned folder's tickers. Re-read only when the file changes (the strip asks every few seconds)."""
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        cached = getattr(self, "_pinned_cache", None)
        if stamp is not None and cached is not None and cached[0] == stamp:
            return list(cached[1])
        try:
            doc = self.load()
        except WatchlistError:
            return []
        f = next((f for f in doc["folders"] if f.get("pinned")), None)
        syms = [str(s) for s in (f or {}).get("symbols", [])]
        try:
            st = self.path.stat()
            self._pinned_cache = ((st.st_mtime_ns, st.st_size), syms)
        except OSError:
            pass
        return syms

    def folder(self, folder_id: str) -> dict[str, Any]:
        doc = self.load()
        f = next((f for f in doc["folders"] if f.get("id") == folder_id), None)
        if f is None:
            raise InputError(f"no folder '{folder_id}'")
        return f

    async def quotes(self, folder_id: str) -> dict[str, Any]:
        """Last price, change and change % (vs previous business day close) for one folder's tickers."""
        from app.explore.views import explore_of   # shared on-demand cache and per-key locks

        f = self.folder(folder_id)
        syms = tuple(str(s).upper() for s in f.get("symbols", []))
        svc = self.svc
        out: dict[str, Any] = {"folder": f["id"], "items": [], "prev_close_pending": 0, "meta": None}
        if not syms:
            return out
        ex = explore_of(svc)
        poll = svc.config.polling.quotes_seconds

        async def load(c: Any) -> tuple[tuple[str, ...], list[Any]]:
            return syms, await c.prices(list(syms))

        e = await ex._fetch(f"watch_prices:{f['id']}", poll, "real-time", load,
                            valid=lambda v: v is not None and v[0] == syms)
        prices = {p.symbol.upper(): p for p in (e.value[1] if e.value is not None and e.value[0] == syms else [])}
        prev_day = svc.calendar.previous_business_day.date if svc.calendar else None
        if prev_day is not None and svc.toss is not None:
            now = time.monotonic()
            paused = now - getattr(svc, "_prev_close_failed_at", float("-inf")) < PREV_CLOSE_RETRY_SECONDS
            missing = [] if paused else [s for s in syms if (s, prev_day) not in svc.prev_close]
            for s in missing[:PREV_CLOSE_PER_REQUEST]:
                try:
                    svc.prev_close[(s, prev_day)] = await svc._fetch_prev_close(svc.toss, s, prev_day)
                except Exception as exc:  # noqa: BLE001 - not stored: all lookups pause PREV_CLOSE_RETRY_SECONDS
                    svc._prev_close_failed_at = now  # type: ignore[attr-defined]
                    log.info("prev close %s failed: %s", s, error_info(exc)[0])
                    break                              # Toss is failing: don't hold this request for the rest
            out["prev_close_pending"] = sum(1 for s in syms if (s, prev_day) not in svc.prev_close)
        for s in syms:
            p = prices.get(s)
            prev = svc.prev_close.get((s, prev_day)) if prev_day else None
            last = p.last_price if p else None
            change = last - prev if last is not None and prev is not None else None
            pct = (change / prev * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if change is not None and prev else None
            listed = svc.stocks.get(s) if svc.stocks else None
            out["items"].append({"symbol": s, "name": listed.name if listed else None, "last_price": last,
                                 "currency": p.currency if p else None, "price_time": p.timestamp if p else None,
                                 "prev_close": prev, "prev_close_date": prev_day, "change": change, "change_pct": pct})
        out["meta"] = ex.meta(e)
        return out


def _format_only(raw: str) -> str:
    if not raw or len(raw) > 20 or not SYMBOL_RE.match(raw):
        raise InputError(f"'{raw}' is not a ticker (letters, digits, '.' or '-')")
    return raw.upper()


def watchlist_of(svc: Services) -> Watchlist:
    w = getattr(svc, "_watchlist", None)
    if w is None:
        w = Watchlist(svc)
        svc._watchlist = w  # type: ignore[attr-defined]
    return w
