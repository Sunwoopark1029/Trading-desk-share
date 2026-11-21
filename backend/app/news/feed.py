"""News feed: fetched by code (Finnhub company-news), normalized, de-duplicated, stored.

Finnhub facts (official swagger, finnhub.io/static/swagger.json, read 2026-11-17):
  GET https://finnhub.io/api/v1/company-news?symbol=&from=YYYY-MM-DD&to=YYYY-MM-DD
  key in header X-Finnhub-Token (so URLs never contain it); North American companies only;
  item fields: category, datetime, headline, id, image, related, source, summary, url.
  The unit of `datetime` is NOT documented: values are accepted only if they are plausible Unix
  seconds; anything else is shown as "date unverified" (never guessed).
Massive (backup) is OFF: its API base URL was not confirmed in the docs (UNVERIFIED).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import truststore

from app.settings import REPO_ROOT, NewsConfig
from app.timeutil import KST

log = logging.getLogger("desk.news")

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
NEWS_DB_PATH = REPO_ROOT / "logs" / "news" / "news.sqlite"
EPOCH_2000 = 946_684_800
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,20}$")


def finnhub_tls_context() -> ssl.SSLContext:
    """Certificates trusted by the operating system (Windows store), for Finnhub only.

    A company security proxy (Zscaler) re-signs finnhub.io traffic with its own certificate,
    which Windows trusts but Python's built-in bundle (certifi) does not. Still fully verified
    (hostname + chain); just against the OS list. Toss/FRED/Anthropic keep the strict bundle.
    """
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class NewsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class NewsItem:
    id: str
    headline: str
    source: str
    url: str
    published: float | None          # Unix seconds (UTC); None = date unverified
    tickers: list[str]
    summary: str = ""
    provider: str = "finnhub"
    first_seen: float = 0.0
    tag: dict[str, Any] | None = None   # priority / direction / reason_en / reason_ko

    @property
    def date_verified(self) -> bool:
        return self.published is not None

    def view(self) -> dict[str, Any]:
        d = asdict(self)
        d["published_kst"] = (datetime.fromtimestamp(self.published, UTC).astimezone(KST).isoformat()
                              if self.published is not None else None)
        d["date_verified"] = self.date_verified
        return d


# ── parsing helpers (unit tested) ───────────────────────────────────────

def parse_epoch_seconds(value: Any, now: float) -> float | None:
    """Plausible Unix seconds only (2000-01-01 .. now + 1 day); anything else -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if EPOCH_2000 <= v <= now + 86400 else None


def url_key(url: str) -> str:
    """Same article with tracking params / www / trailing slash -> same key."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = p.netloc.lower().removeprefix("www.")
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")])
    return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/"), q, ""))


def title_key(headline: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", headline.lower()))


def normalize_finnhub(raw: Any, symbol: str, now: float) -> NewsItem | None:
    """One Finnhub item -> NewsItem, or None if unusable (no headline or no http(s) url)."""
    if not isinstance(raw, dict):
        return None
    headline = str(raw.get("headline") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not headline or not url.lower().startswith(("http://", "https://")):
        return None
    tickers = {symbol}
    for t in str(raw.get("related") or "").replace(" ", "").split(","):
        if SYMBOL_RE.match(t.upper()):
            tickers.add(t.upper())
    item_id = "fh:" + hashlib.sha1(url_key(url).encode()).hexdigest()[:16]
    return NewsItem(id=item_id, headline=headline[:500], source=str(raw.get("source") or "")[:100],
                    url=url[:2000], published=parse_epoch_seconds(raw.get("datetime"), now),
                    tickers=sorted(tickers), summary=str(raw.get("summary") or "")[:800],
                    provider="finnhub", first_seen=now)


def dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Merge items with the same URL key or the same headline; tickers are combined."""
    out: list[NewsItem] = []
    by_url: dict[str, NewsItem] = {}
    by_title: dict[str, NewsItem] = {}
    for it in items:
        uk, tk = url_key(it.url), title_key(it.headline)
        hit = by_url.get(uk) or (by_title.get(tk) if tk else None)
        if hit is not None:
            hit.tickers = sorted(set(hit.tickers) | set(it.tickers))
            if hit.published is None and it.published is not None:
                hit.published = it.published
            continue
        out.append(it)
        by_url[uk] = it
        if tk:
            by_title[tk] = it
    return out


def is_fresh(it: NewsItem, now: float, max_age_hours: float) -> bool:
    ref = it.published if it.published is not None else it.first_seen
    return now - ref <= max_age_hours * 3600


# ── store ────────────────────────────────────────────────────────────────

class NewsStore:
    """logs/news/news.sqlite: items + their AI tags (so nothing is tagged twice)."""

    def __init__(self, path: Path = NEWS_DB_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, url_key TEXT, title_key TEXT, "
                      "data TEXT NOT NULL, ref REAL NOT NULL, tag TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_items_ref ON items(ref)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, items: list[NewsItem]) -> int:
        """Insert new items; for known ones (same url or headline) merge tickers. Returns new count."""
        new = 0
        with self._lock, self._conn() as c:
            for it in items:
                uk, tk = url_key(it.url), title_key(it.headline)
                row = c.execute("SELECT id, data FROM items WHERE id = ? OR url_key = ? OR (title_key = ? AND ? != '')",
                                (it.id, uk, tk, tk)).fetchone()
                if row is None:
                    ref = it.published if it.published is not None else it.first_seen
                    c.execute("INSERT INTO items (id, url_key, title_key, data, ref) VALUES (?, ?, ?, ?, ?)",
                              (it.id, uk, tk, json.dumps(asdict(it)), ref))
                    new += 1
                else:
                    old = json.loads(row["data"])
                    merged = sorted(set(old.get("tickers", [])) | set(it.tickers))
                    if merged != old.get("tickers"):
                        old["tickers"] = merged
                        c.execute("UPDATE items SET data = ? WHERE id = ?", (json.dumps(old), row["id"]))
        return new

    def _rows(self, where: str, args: tuple[Any, ...]) -> list[NewsItem]:
        with self._lock, self._conn() as c:
            rows = c.execute(f"SELECT data, tag FROM items WHERE {where}", args).fetchall()
        out = []
        for r in rows:
            try:
                d = json.loads(r["data"])
                d["tag"] = json.loads(r["tag"]) if r["tag"] else None
                out.append(NewsItem(**d))
            except (TypeError, ValueError):
                continue  # a broken row never breaks the feed
        return out

    def recent(self, since: float) -> list[NewsItem]:
        return self._rows("ref >= ?", (since,))

    def untagged(self, since: float, limit: int) -> list[NewsItem]:
        return self._rows("ref >= ? AND tag IS NULL ORDER BY ref DESC LIMIT ?", (since, limit))

    def set_tags(self, tags: dict[str, dict[str, Any]]) -> None:
        with self._lock, self._conn() as c:
            for item_id, tag in tags.items():
                c.execute("UPDATE items SET tag = ? WHERE id = ?", (json.dumps(tag, ensure_ascii=False), item_id))


# ── fetching ─────────────────────────────────────────────────────────────

async def fetch_finnhub(http: httpx.AsyncClient, key: str, symbol: str, now: float,
                        max_age_hours: float) -> list[Any]:
    """Raw items. Errors -> NewsError(code) without URL or key in the message."""
    start = datetime.fromtimestamp(now - max_age_hours * 3600, UTC).date()
    end = (datetime.fromtimestamp(now, UTC) + timedelta(days=1)).date()
    try:
        resp = await http.get(FINNHUB_URL, params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
                              headers={"X-Finnhub-Token": key})
    except httpx.HTTPError as exc:
        raise NewsError(f"network:{type(exc).__name__}") from None
    if resp.status_code == 429:
        raise NewsError("rate-limited")
    if resp.status_code in (401, 403):
        raise NewsError(f"auth:{resp.status_code}")
    if resp.status_code != 200:
        raise NewsError(f"http:{resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise NewsError("bad-json") from None
    if not isinstance(data, list):
        raise NewsError("bad-shape")
    return data


@dataclass
class SymbolStatus:
    last_try: float = 0.0
    last_ok: float | None = None
    error: str | None = None
    count: int = 0


@dataclass
class NewsFeed:
    cfg: NewsConfig
    key: str
    store: NewsStore
    watch: Callable[[], list[str]]          # tickers to follow (screen, holdings, related, extra)
    http: httpx.AsyncClient | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Any] = asyncio.sleep
    status: dict[str, SymbolStatus] = field(default_factory=dict)
    _last_call: float = 0.0
    gate: Any = None    # phase 6: the app-wide FinnhubGate (shared with fundamentals); None = own spacing

    def watch_list(self) -> list[str]:
        out: list[str] = []
        for s in self.watch():
            for t in [s, *self.cfg.related.get(s, [])]:
                t = t.upper()
                if SYMBOL_RE.match(t) and t not in out:
                    out.append(t)
        for t in self.cfg.extra:
            if SYMBOL_RE.match(t.upper()) and t.upper() not in out:
                out.append(t.upper())
        return out[: self.cfg.max_symbols]

    async def poll_once(self) -> int:
        """Fetch every watched ticker whose last try is older than poll_seconds. Returns new items."""
        if not self.key or not self.cfg.enabled:
            return 0
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=10, verify=finnhub_tls_context())
        new = 0
        for sym in self.watch_list():
            st = self.status.setdefault(sym, SymbolStatus())
            now = self.clock()
            if now - st.last_try < self.cfg.poll_seconds:
                continue
            if self.gate is not None:
                try:
                    await self.gate.acquire()           # waits its turn with every other Finnhub call
                except Exception:  # noqa: BLE001 - paused after a 429 somewhere in the app: stop this round
                    break
            else:
                wait = self.cfg.finnhub_min_interval_seconds - (now - self._last_call)
                if wait > 0:
                    await self.sleep(wait)
            st.last_try = self._last_call = self.clock()
            try:
                raw = await fetch_finnhub(self.http, self.key, sym, self.clock(), self.cfg.max_age_hours)
            except NewsError as exc:
                st.error = exc.code
                log.warning("news %s: %s", sym, exc.code)
                if exc.code == "rate-limited":
                    if self.gate is not None:
                        self.gate.hit_429()             # pauses fundamentals calls too
                    break
                continue
            items = [x for x in (normalize_finnhub(r, sym, self.clock()) for r in raw) if x is not None]
            items = [x for x in dedupe(items) if is_fresh(x, self.clock(), self.cfg.max_age_hours)]
            n = self.store.upsert(items)
            st.last_ok, st.error, st.count = self.clock(), None, len(items)
            new += n
        return new

    def items(self, symbols: set[str] | None = None) -> list[NewsItem]:
        now = self.clock()
        its = [i for i in self.store.recent(now - self.cfg.max_age_hours * 3600)
               if is_fresh(i, now, self.cfg.max_age_hours)]
        if symbols:
            its = [i for i in its if symbols & set(i.tickers)]
        return sorted(its, key=lambda i: (i.published or i.first_seen), reverse=True)

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
