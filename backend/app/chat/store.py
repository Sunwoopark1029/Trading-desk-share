"""Chat history in logs/chat/chat.sqlite (gitignored). Standard library sqlite3 only.

Each row is one API message (user or assistant) exactly as sent/received, so the history can be
replayed byte-for-byte (append-only: keeps the prompt cache and the model's earlier reasoning valid).
`display` holds what the screen shows (question text, answer text, scenario table, cost line).
`active = 0` marks rows replaced by a summary (kept for the record, no longer sent).

Phase 6 part C: each conversation belongs to one ticker (`symbol`) and one page (`kind`: desk / research).
Older databases are upgraded once (columns added, ticker filled in from the first question); nothing is deleted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.settings import REPO_ROOT

CHAT_DB_PATH = REPO_ROOT / "logs" / "chat" / "chat.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  summary TEXT,                 -- Haiku summary of rows marked inactive
  state TEXT NOT NULL DEFAULT '{}',  -- JSON: context already sent (ticker/date), last bar sent, ...
  symbol TEXT,                  -- the ticker this chat is about (phase 6 part C); NULL until known
  kind TEXT NOT NULL DEFAULT 'desk'  -- desk | research
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  turn_id TEXT NOT NULL,        -- one user question + everything it caused
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,        -- JSON list of API content blocks
  display TEXT,                 -- JSON for the screen, NULL for internal rows (tool results)
  active INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conversation_id, id);
"""

KINDS = ("desk", "research")
SCHEMA_VERSION = 1


def _first_symbol(c: sqlite3.Connection, cid: str, state_json: str | None) -> str | None:
    """The ticker of an old chat: its first question's screen ticker, else the one in its saved context key."""
    for (disp,) in c.execute("SELECT display FROM messages WHERE conversation_id = ? AND display IS NOT NULL "
                             "ORDER BY id", (cid,)):
        try:
            d = json.loads(disp)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict) and d.get("kind") == "question" and isinstance(d.get("symbol"), str) and d["symbol"]:
            return d["symbol"].upper()
    try:
        key = json.loads(state_json or "{}").get("context_key")
    except (TypeError, ValueError, AttributeError):
        key = None
    if isinstance(key, str) and "|" in key and key.split("|", 1)[0]:
        return key.split("|", 1)[0].upper()
    return None


def migrate(c: sqlite3.Connection) -> None:
    """One-time upgrade of an older chat database: add symbol / kind, fill in the ticker. Safe to run again."""
    if int(c.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
        return
    cols = {r[1] for r in c.execute("PRAGMA table_info(conversations)")}
    if "symbol" not in cols:
        c.execute("ALTER TABLE conversations ADD COLUMN symbol TEXT")
    if "kind" not in cols:
        c.execute("ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'desk'")
    for cid, state in c.execute("SELECT id, state FROM conversations WHERE symbol IS NULL").fetchall():
        sym = _first_symbol(c, cid, state)
        if sym:
            c.execute("UPDATE conversations SET symbol = ? WHERE id = ?", (sym, cid))
    c.execute("CREATE INDEX IF NOT EXISTS ix_conv_symbol ON conversations(symbol, kind, updated)")
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ChatStore:
    def __init__(self, path: Path = CHAT_DB_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            migrate(c)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ── conversations ────────────────────────────────────────────────────

    def create(self, title: str = "", symbol: str | None = None, kind: str = "desk") -> str:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        cid = uuid.uuid4().hex[:16]
        now = _now()
        with self._conn() as c:
            c.execute("INSERT INTO conversations (id, title, created, updated, symbol, kind) VALUES (?, ?, ?, ?, ?, ?)",
                      (cid, title[:200], now, now, symbol.upper() if symbol else None, kind))
        return cid

    def list(self, symbol: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT id, title, created, updated, symbol, kind FROM conversations", []
        where = []
        if symbol is not None:
            where.append("symbol = ?")
            args.append(symbol.upper())
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if where:
            q += " WHERE " + " AND ".join(where)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY updated DESC", args).fetchall()
        return [dict(r) for r in rows]

    def set_symbol(self, cid: str, symbol: str) -> None:
        """Give a chat without a ticker yet its ticker (never changes an existing one)."""
        with self._conn() as c:
            c.execute("UPDATE conversations SET symbol = ? WHERE id = ? AND symbol IS NULL", (symbol.upper(), cid))

    def get(self, cid: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["state"] = json.loads(d["state"] or "{}")
        return d

    def delete(self, cid: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        return cur.rowcount > 0

    def set_title(self, cid: str, title: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE conversations SET title = ? WHERE id = ?", (title[:200], cid))

    def set_state(self, cid: str, state: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("UPDATE conversations SET state = ?, updated = ? WHERE id = ?",
                      (json.dumps(state, sort_keys=True), _now(), cid))

    # ── messages ─────────────────────────────────────────────────────────

    def add(self, cid: str, turn_id: str, role: str, content: list[dict[str, Any]],
            display: dict[str, Any] | None = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages (conversation_id, turn_id, role, content, display, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, turn_id, role, json.dumps(content, ensure_ascii=False),
                 None if display is None else json.dumps(display, ensure_ascii=False), _now()))
            c.execute("UPDATE conversations SET updated = ? WHERE id = ?", (_now(), cid))
            return int(cur.lastrowid or 0)

    def set_display(self, msg_id: int, display: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("UPDATE messages SET display = ? WHERE id = ?",
                      (json.dumps(display, ensure_ascii=False), msg_id))

    def history(self, cid: str) -> list[dict[str, Any]]:
        """Active rows as API messages, in order."""
        with self._conn() as c:
            rows = c.execute("SELECT role, content FROM messages WHERE conversation_id = ? AND active = 1 "
                             "ORDER BY id", (cid,)).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def rows(self, cid: str, active_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM messages WHERE conversation_id = ?"
        if active_only:
            q += " AND active = 1"
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id", (cid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["content"] = json.loads(d["content"])
            d["display"] = json.loads(d["display"]) if d["display"] else None
            out.append(d)
        return out

    def compact(self, cid: str, summary: str | None, keep_from_id: int,
                kept_rewrites: dict[int, list[dict[str, Any]]]) -> None:
        """Rows before keep_from_id become inactive; kept rows may get new content
        (thinking stripped, summary prepended). One transaction."""
        with self._conn() as c:
            c.execute("UPDATE messages SET active = 0 WHERE conversation_id = ? AND id < ?", (cid, keep_from_id))
            for mid, content in kept_rewrites.items():
                c.execute("UPDATE messages SET content = ? WHERE id = ? AND conversation_id = ?",
                          (json.dumps(content, ensure_ascii=False), mid, cid))
            c.execute("UPDATE conversations SET summary = ?, updated = ? WHERE id = ?", (summary, _now(), cid))
