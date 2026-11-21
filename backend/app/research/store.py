"""Thesis + notes per ticker (phase 6 part C, step C4) in logs/research/research.sqlite (gitignored).

- Thesis: one current thesis per ticker, older ones kept as history. Built only from a valuation card the code made
  (its numbers come from the fundamentals pack); saved by the user's button, or by the AI when the user's own message
  asks for it (user choice 2026-11-21, checked in code). The user may edit the summary and which reference prices
  show on the chart.
- Notes: short dated lines the user types (or pins), plus facts code knows for certain ("thesis saved"). No LLM
  writes notes. Capped per ticker (oldest dropped first).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.settings import REPO_ROOT

RESEARCH_DB_PATH = REPO_ROOT / "logs" / "research" / "research.sqlite"
NOTE_MAX_CHARS = 300
NOTES_CAP_CHARS = 6000            # ~1,500 tokens per ticker; oldest notes go first
NOTE_SOURCES = ("user", "pinned", "code")
SAVED_BY = ("user", "ai")

SCHEMA = """
CREATE TABLE IF NOT EXISTS theses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  saved_at TEXT NOT NULL,
  saved_by TEXT NOT NULL,       -- user | ai (on the user's request)
  current INTEGER NOT NULL DEFAULT 1,
  data TEXT NOT NULL            -- JSON thesis
);
CREATE INDEX IF NOT EXISTS ix_theses_symbol ON theses(symbol, current, id);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  created TEXT NOT NULL,
  source TEXT NOT NULL,         -- user | pinned | code
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_symbol ON notes(symbol, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ResearchStore:
    def __init__(self, path: Path = RESEARCH_DB_PATH, clock: Any = None) -> None:
        self.path = path
        self.clock = clock or _now
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ── thesis ───────────────────────────────────────────────────────────

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return {"id": r["id"], "symbol": r["symbol"], "saved_at": r["saved_at"], "saved_by": r["saved_by"],
                "current": bool(r["current"]), **json.loads(r["data"])}

    def current(self, symbol: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM theses WHERE symbol = ? AND current = 1 ORDER BY id DESC LIMIT 1",
                          (symbol.upper(),)).fetchone()
        return None if r is None else self._row(r)

    def history(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM theses WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                             (symbol.upper(), limit)).fetchall()
        out = []
        for r in rows:
            d = self._row(r)
            out.append({k: d.get(k) for k in ("id", "saved_at", "saved_by", "current", "verdict", "summary",
                                              "price_at_save", "currency")})
        return out

    def save(self, symbol: str, data: dict[str, Any], saved_by: str) -> dict[str, Any]:
        if saved_by not in SAVED_BY:
            raise ValueError("saved_by must be user or ai")
        sym = symbol.upper()
        with self._conn() as c:
            c.execute("UPDATE theses SET current = 0 WHERE symbol = ?", (sym,))
            cur = c.execute("INSERT INTO theses (symbol, saved_at, saved_by, current, data) VALUES (?, ?, ?, 1, ?)",
                            (sym, self.clock(), saved_by, json.dumps(data, ensure_ascii=False, sort_keys=True)))
            tid = int(cur.lastrowid or 0)
            r = c.execute("SELECT * FROM theses WHERE id = ?", (tid,)).fetchone()
        return self._row(r)

    def clear(self, symbol: str) -> bool:
        """No current thesis any more (history kept)."""
        with self._conn() as c:
            cur = c.execute("UPDATE theses SET current = 0 WHERE symbol = ? AND current = 1", (symbol.upper(),))
        return cur.rowcount > 0

    # ── notes ────────────────────────────────────────────────────────────

    def notes(self, symbol: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id, created, source, text FROM notes WHERE symbol = ? ORDER BY id",
                             (symbol.upper(),)).fetchall()
        return [dict(r) for r in rows]

    def add_note(self, symbol: str, text: str, source: str = "user") -> dict[str, Any]:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("note is empty")
        if len(text) > NOTE_MAX_CHARS:
            raise ValueError(f"note is too long (max {NOTE_MAX_CHARS} characters)")
        if source not in NOTE_SOURCES:
            raise ValueError("source must be user, pinned or code")
        sym = symbol.upper()
        with self._conn() as c:
            cur = c.execute("INSERT INTO notes (symbol, created, source, text) VALUES (?, ?, ?, ?)",
                            (sym, self.clock(), source, text))
            nid = int(cur.lastrowid or 0)
            rows = c.execute("SELECT id, text FROM notes WHERE symbol = ? ORDER BY id DESC", (sym,)).fetchall()
            total, keep = 0, set()
            for r in rows:                              # newest first; oldest beyond the cap are dropped
                total += len(r["text"])
                if total <= NOTES_CAP_CHARS or r["id"] == nid:
                    keep.add(r["id"])
            drop = [r["id"] for r in rows if r["id"] not in keep]
            if drop:
                c.executemany("DELETE FROM notes WHERE id = ?", [(i,) for i in drop])
            r = c.execute("SELECT id, created, source, text FROM notes WHERE id = ?", (nid,)).fetchone()
        return dict(r)

    def delete_note(self, symbol: str, note_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM notes WHERE id = ? AND symbol = ?", (note_id, symbol.upper()))
        return cur.rowcount > 0

    def memory_version(self, symbol: str) -> str:
        """Changes when the current thesis or the notes change (the chat context is then sent again)."""
        cur = self.current(symbol)
        ids = [n["id"] for n in self.notes(symbol)]
        raw = json.dumps([cur["id"] if cur else None, cur.get("summary") if cur else None, ids])
        return hashlib.sha1(raw.encode()).hexdigest()[:10]


def research_store_of(svc: Any) -> ResearchStore:
    st = getattr(svc, "_research_store", None)
    if st is None:
        st = ResearchStore(RESEARCH_DB_PATH)
        svc._research_store = st
    return st
