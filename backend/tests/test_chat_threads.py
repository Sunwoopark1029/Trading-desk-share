"""Phase 6 part C step C1: one ticker per chat, chat kinds, the one-time database upgrade, usage fields."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.store import ChatStore
from app.main import create_app
from tests.test_chat_engine import LOCAL, SCREEN, FakeClient, make_engine, ready_services, usage

OLD_SCHEMA = """
CREATE TABLE conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL,
  summary TEXT, state TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  turn_id TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL, display TEXT, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL
);
CREATE INDEX ix_messages_conv ON messages(conversation_id, id);
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _old_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    rows = [("a1", "SOXS chat", "{}"), ("b2", "context only", json.dumps({"context_key": "nvda|2026-11-20|abcd1234"})),
            ("c3", "empty", "{}")]
    for cid, title, state in rows:
        c.execute("INSERT INTO conversations (id, title, created, updated, state) VALUES (?, ?, 't', 't', ?)",
                  (cid, title, state))
    c.execute("INSERT INTO messages (conversation_id, turn_id, role, content, display, created) VALUES "
              "('a1', 't1', 'user', '[]', ?, 't')", (json.dumps({"kind": "question", "text": "hi", "symbol": "soxs"}),))
    c.execute("INSERT INTO messages (conversation_id, turn_id, role, content, display, created) VALUES "
              "('a1', 't2', 'user', '[]', ?, 't')", (json.dumps({"kind": "question", "text": "x", "symbol": "AAPL"}),))
    c.execute("INSERT INTO messages (conversation_id, turn_id, role, content, display, created) VALUES "
              "('b2', 't1', 'assistant', '[]', ?, 't')", (json.dumps({"kind": "answer", "text": "ok"}),))
    c.commit()
    c.close()


def test_old_database_is_upgraded_once_and_keeps_everything(tmp_path: Path) -> None:
    db = tmp_path / "chat.sqlite"
    _old_db(db)
    st = ChatStore(db)
    by = {c["id"]: c for c in st.list()}
    assert by["a1"]["symbol"] == "SOXS"           # first question's ticker, upper-cased
    assert by["b2"]["symbol"] == "NVDA"           # no question: the saved context key
    assert by["c3"]["symbol"] is None             # nothing known: stays unset
    assert {c["kind"] for c in by.values()} == {"desk"}
    assert len(st.rows("a1")) == 2 and st.get("a1")["title"] == "SOXS chat"
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    st.set_symbol("a1", "TSLA")                   # never changes a known ticker
    st.set_symbol("c3", "aapl")                   # fills an unknown one
    st2 = ChatStore(db)                           # second open: no re-run, nothing changes
    by2 = {c["id"]: c for c in st2.list()}
    assert by2["a1"]["symbol"] == "SOXS" and by2["c3"]["symbol"] == "AAPL"


def test_create_and_filter_by_ticker_and_kind(tmp_path: Path) -> None:
    st = ChatStore(tmp_path / "chat.sqlite")
    a = st.create(symbol="nvda")
    b = st.create(symbol="NVDA", kind="research")
    c = st.create(symbol="SOXS")
    assert [x["id"] for x in st.list("NVDA", "desk")] == [a]
    assert [x["id"] for x in st.list("nvda", "research")] == [b]
    assert {x["id"] for x in st.list()} == {a, b, c}
    assert st.get(b)["kind"] == "research" and st.get(a)["symbol"] == "NVDA"
    with pytest.raises(ValueError):
        st.create(kind="discover")


@pytest.mark.anyio
async def test_routes_one_ticker_per_chat(tmp_path: Path) -> None:
    svc = await ready_services(tmp_path)
    client = FakeClient([([{"type": "text", "text": "Fine."}], "end_turn", usage())])
    eng = make_engine(svc, tmp_path, client)
    with TestClient(create_app(svc, start_pollers=False, chat=eng), base_url=LOCAL) as c:
        assert c.post("/api/chat/conversations", json={"kind": "other"}).status_code == 400
        assert c.post("/api/chat/conversations", json={"symbol": "NOPE!!"}).status_code == 400
        assert c.post("/api/chat/conversations", json=[1]).status_code == 400
        r_id = c.post("/api/chat/conversations", json={"symbol": "AAPL", "kind": "research"}).json()["id"]
        blank = c.post("/api/chat/conversations").json()["id"]          # no body: desk, ticker unknown
        lst = c.get("/api/chat/conversations", params={"symbol": "AAPL", "kind": "research"}).json()["conversations"]
        assert [x["id"] for x in lst] == [r_id] and lst[0]["kind"] == "research"
        assert c.get("/api/chat/conversations", params={"kind": "x"}).status_code == 400
        # format-only check for listing: works before the Toss stock list has loaded
        assert c.get("/api/chat/conversations", params={"symbol": "ZZZZ", "kind": "desk"}).json() == {"conversations": []}
        got = c.get(f"/api/chat/conversations/{r_id}").json()
        assert got["symbol"] == "AAPL" and got["kind"] == "research"

        # a chat about SOXX refuses a question asked from the SOXS screen (D5)
        r = c.post(f"/api/chat/conversations/{r_id}/messages", json={"text": "hi", "tier": "normal", "screen": SCREEN})
        assert r.status_code == 409 and r.json()["chat_symbol"] == "AAPL"

        # a chat without a ticker takes the ticker of its first question
        r = c.post(f"/api/chat/conversations/{blank}/messages", json={"text": "hi", "tier": "normal", "screen": SCREEN})
        assert r.status_code == 200 and '"type": "done"' in r.text
        assert c.get(f"/api/chat/conversations/{blank}").json()["symbol"] == "SOXS"
        assert [x["id"] for x in c.get("/api/chat/conversations",
                                       params={"symbol": "SOXS", "kind": "desk"}).json()["conversations"]] == [blank]
    rec: dict[str, Any] = eng.usage.records()[-1]
    assert rec["symbol"] == "SOXS" and rec["kind"] == "desk" and rec["conversation_id"] == blank
