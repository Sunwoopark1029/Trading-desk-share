"""Phase 6 part C step C4: thesis + notes per ticker (store, routes, AI save only when asked, memory in chat)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.engine import TurnRequest
from app.main import create_app
from app.research.store import ResearchStore
from app.research.thesis import asked_to_save
from tests.test_chat_engine import LOCAL, FakeClient, make_engine, usage
from tests.test_research import setup
from tests.test_research_chat import _args


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("text,ok", [
    ("save it", True), ("please save this thesis", True), ("저장해줘", True), ("기록해줘", True), ("남겨줘", True),
    ("don't save it", False), ("do not save", False), ("저장하지 마", False), ("저장 말고 설명만", False),
    ("is it cheap?", False), ("saved me a lot", False), ("unsaved", False),
    ("ok, save it.", True), ("go ahead and save", True), ("저장해 둬", True), ("저장 부탁해", True),
    ("Don’t save", False), ("no need to save it", False), ("저장은 하지 마", False), ("저장할 필요 없어", False),
    ("저장 ㄴㄴ", False), ("Should I save this?", False), ("저장된 thesis 보여줘", False), ("저장해둔 거랑 비교해줘", False),
    ("기록해둔 노트 보여줘", False), ("How are Apple's store sales trending?", False), ("Is it a store of value?", False),
])
def test_save_only_when_the_user_asks(text: str, ok: bool) -> None:
    assert asked_to_save(text) is ok


def test_store_thesis_history_and_notes_cap(tmp_path: Path) -> None:
    st = ResearchStore(tmp_path / "r.sqlite")
    v0 = st.memory_version("AAPL")
    a = st.save("aapl", {"summary": "one", "verdict": {}}, "user")
    b = st.save("AAPL", {"summary": "two", "verdict": {}}, "ai")
    assert st.current("AAPL")["id"] == b["id"] and st.current("AAPL")["saved_by"] == "ai"
    assert [h["summary"] for h in st.history("AAPL")] == ["two", "one"] and not st.history("AAPL")[1]["current"]
    v1 = st.memory_version("AAPL")
    assert v1 != v0
    assert st.clear("AAPL") and st.current("AAPL") is None and len(st.history("AAPL")) == 2
    with pytest.raises(ValueError):
        st.save("AAPL", {}, "someone")
    with pytest.raises(ValueError):
        st.add_note("AAPL", "   ")
    with pytest.raises(ValueError):
        st.add_note("AAPL", "x" * 301)
    for i in range(30):                                   # 30 x 290 chars > 6000: the oldest go first
        st.add_note("AAPL", f"{i:02d} " + "y" * 287)
    notes = st.notes("AAPL")
    assert sum(len(n["text"]) for n in notes) <= 6000 and notes[-1]["text"].startswith("29")
    assert not any(n["text"].startswith("00") for n in notes)
    assert st.delete_note("AAPL", notes[0]["id"]) and not st.delete_note("TSLA", notes[1]["id"])
    assert a["id"] != b["id"]


async def _chat_with_card(svc: Any, rs: Any, tmp_path: Path, client: FakeClient, text: str = "is AAPL cheap?") -> Any:
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create(symbol="AAPL", kind="research")
    events = [ev async for ev in eng.run_turn(cid, TurnRequest(text=text, tier="normal", screen={"symbol": "AAPL"}))]
    return eng, cid, events


@pytest.mark.anyio
async def test_routes_save_from_card_edit_delete_and_notes(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    pack = (await rs.metrics("AAPL"))["pack"]
    tgt = next((v["id"] for v in pack["values"] if v["id"].startswith("target_") and v["value"] is not None), None)
    client = FakeClient([
        ([{"type": "tool_use", "id": "v1", "name": "show_valuation",
           "input": _args(reference_ids=[tgt] if tgt else [])}], "tool_use", usage(2000, 40)),
        ([{"type": "text", "text": "Fair."}], "end_turn", usage(300, 20)),
    ])
    eng, cid, _ = await _chat_with_card(svc, rs, tmp_path, client)
    row = next(r for r in eng.store.rows(cid) if r.get("display") and r["display"].get("valuations"))
    with TestClient(create_app(svc, start_pollers=False, chat=eng), base_url=LOCAL) as c:
        assert c.get("/api/research/AAPL/thesis").json()["thesis"] is None
        bad = c.put("/api/research/AAPL/thesis", json={"summary": "x"})            # nothing to edit yet
        assert bad.status_code == 400
        assert c.put("/api/research/AAPL/thesis", json={"conversation_id": cid, "message_id": row["id"],
                                                         "card_index": 5}).status_code == 400
        assert c.put("/api/research/NVDA/thesis", json={"conversation_id": cid, "message_id": row["id"],
                                                         "card_index": 0}).status_code == 400   # card is about AAPL
        r = c.put("/api/research/AAPL/thesis", json={"conversation_id": cid, "message_id": row["id"], "card_index": 0,
                                                      "summary": "My own words.", "price": "1"})
        assert r.status_code == 200
        view = r.json()
        th = view["thesis"]
        assert th["summary"] == "My own words." and th["saved_by"] == "user" and th["verdict"]["growth"] == "fair"
        assert th["price_at_save"] == row["display"]["valuations"][0]["price"]["last"]   # from the card, not the body
        assert view["facts"]["moved_since_save"]["display"] in ("0.0%", "+0.0%", "−0.0%")
        if tgt:
            assert th["reference_levels"][0]["show_on_chart"] is False                 # targets off by default
            r = c.put("/api/research/AAPL/thesis", json={"show_on_chart": {tgt: True}})
            assert r.json()["thesis"]["reference_levels"][0]["show_on_chart"] is True
            assert r.json()["thesis"]["summary"] == "My own words." and len(r.json()["history"]) == 2
        assert any(n["source"] == "code" and n["text"].startswith("Thesis saved by you") for n in view["notes"])
        n = c.post("/api/research/AAPL/notes", json={"text": "Watch the gross margin next quarter."}).json()["note"]
        assert c.post("/api/research/AAPL/notes", json={"text": "x", "source": "code"}).status_code == 400
        assert any(x["id"] == n["id"] for x in c.get("/api/research/AAPL/notes").json()["notes"])
        assert c.delete(f"/api/research/AAPL/notes/{n['id']}").json() == {"ok": True}
        assert c.delete(f"/api/research/AAPL/notes/{n['id']}").status_code == 404
        assert c.delete("/api/research/AAPL/thesis").json() == {"ok": True}
        assert c.get("/api/research/AAPL/thesis").json()["thesis"] is None


@pytest.mark.anyio
async def test_ai_saves_only_on_request_and_memory_reaches_the_chat(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    client = FakeClient([
        # turn 1: card + a save attempt the user did not ask for
        ([{"type": "tool_use", "id": "v1", "name": "show_valuation", "input": _args()},
          {"type": "tool_use", "id": "s1", "name": "save_thesis", "input": {"summary": None}}],
         "tool_use", usage(2000, 40)),
        ([{"type": "text", "text": "Fair."}], "end_turn", usage(300, 20)),
        # turn 2: the user asks to save -> saves the card from turn 1
        ([{"type": "tool_use", "id": "s2", "name": "save_thesis", "input": {"summary": None}}],
         "tool_use", usage(300, 20)),
        ([{"type": "text", "text": "Saved."}], "end_turn", usage(300, 20)),
        # turn 3: memory now in the context block
        ([{"type": "text", "text": "ok"}], "end_turn", usage(300, 20)),
    ])
    eng, cid, ev1 = await _chat_with_card(svc, rs, tmp_path, client)
    refused = client.calls[1]["messages"][-1]["content"][1]
    assert refused["is_error"] and "does not ask to save" in refused["content"]
    assert not any(e["type"] == "thesis_saved" for e in ev1)
    ev2 = [e async for e in eng.run_turn(cid, TurnRequest(text="좋아, 저장해줘", tier="normal", screen={"symbol": "AAPL"}))]
    assert any(e["type"] == "thesis_saved" for e in ev2)
    from app.research.store import research_store_of
    cur = research_store_of(svc).current("AAPL")
    assert cur["saved_by"] == "ai" and cur["conversation_id"] == cid and cur["summary"] == "Fair against growth."
    [e async for e in eng.run_turn(cid, TurnRequest(text="and now?", tier="normal", screen={"symbol": "AAPL"}))]
    blocks = client.calls[4]["messages"][-1]["content"]
    assert blocks[0]["text"].startswith("<context>") and '"memory"' in blocks[0]["text"]
    assert "Fair against growth." in blocks[0]["text"]


@pytest.mark.anyio
async def test_ai_cannot_save_a_card_with_unverified_numbers(tmp_path: Path) -> None:
    svc, rs, _, _ = await setup(tmp_path)
    client = FakeClient([
        ([{"type": "tool_use", "id": "v1", "name": "show_valuation",
           "input": _args(summary="Fair value USD 312.40, upside 41%.")},
          {"type": "tool_use", "id": "s1", "name": "save_thesis", "input": {}}], "tool_use", usage(2000, 40)),
        ([{"type": "text", "text": "Card made."}], "end_turn", usage(300, 20)),
    ])
    eng, cid, events = await _chat_with_card(svc, rs, tmp_path, client, text="value it and save it")
    res = client.calls[1]["messages"][-1]["content"][1]
    assert res["is_error"] and "not found in the dashboard data" in res["content"] and "41%" in res["content"]
    from app.research.store import research_store_of
    assert research_store_of(svc).current("AAPL") is None
    done = next(e for e in events if e["type"] == "done")["display"]
    assert any("41%" in w for w in done["warnings"])
    # the user can still save it with the button: the thesis then carries the warning
    row = next(r for r in eng.store.rows(cid) if r.get("display") and r["display"].get("valuations"))
    with TestClient(create_app(svc, start_pollers=False, chat=eng), base_url=LOCAL) as c:
        th = c.put("/api/research/AAPL/thesis", json={"conversation_id": cid, "message_id": row["id"],
                                                       "card_index": 0}).json()["thesis"]
    assert th["saved_by"] == "user" and any("41%" in w for w in th["warnings"])


@pytest.mark.anyio
async def test_desk_chat_gets_the_thesis_levels_context_and_tool(tmp_path: Path) -> None:
    from app.chat.levels import compute_levels
    from app.research.store import research_store_of
    svc, _, _, _ = await setup(tmp_path)
    store = research_store_of(svc)
    data = {"symbol": "AAPL", "verdict": {"own_history": "cheap", "peers": "unclear", "growth": "fair"},
            "summary": "Cheap vs its own history; fair value USD 777.77.", "currency": "USD", "price_at_save": "200",
            "reference_levels": [{"id": "fv_pe_own_median", "label": "Price at own median P/E", "price": "150.5",
                                  "display": "USD 150.50", "source": "calculated", "show_on_chart": True}]}
    store.save("AAPL", data, "user")
    ls = await compute_levels(svc, "AAPL", "5m")
    lv = next(x for x in ls.fixed if x.id == "thesis_fv_pe_own_median")
    assert lv.kind == "research" and str(lv.price) == "150.5"

    client = FakeClient([
        ([{"type": "tool_use", "id": "g1", "name": "get_thesis", "input": {}}], "tool_use", usage(2000, 20)),
        ([{"type": "text", "text": "Thesis level 150.50 (thesis_fv_pe_own_median); fair value 777.77."}],
         "end_turn", usage(300, 20)),
        ([{"type": "text", "text": "ok"}], "end_turn", usage(300, 20)),
        ([{"type": "text", "text": "ok"}], "end_turn", usage(300, 20)),
    ])
    eng = make_engine(svc, tmp_path, client)
    cid = eng.store.create(symbol="AAPL")
    ev = [e async for e in eng.run_turn(cid, TurnRequest(text="plan?", tier="normal", screen={"symbol": "AAPL"}))]
    first = client.calls[0]
    assert "get_thesis" in [t["name"] for t in first["tools"]]
    ctx_text = first["messages"][0]["content"][0]["text"]
    assert ctx_text.startswith("<context>") and '"thesis"' in ctx_text and "thesis_fv_pe_own_median" in ctx_text
    res = client.calls[1]["messages"][-1]["content"][0]
    assert not res.get("is_error") and "Cheap vs its own history" in res["content"]
    done = next(e for e in ev if e["type"] == "done")["display"]
    # the level price is code data; a price that only appears in the thesis words (or get_thesis echo) is not
    assert any("777.77" in w for w in done["warnings"]) and not any("150.50" in w for w in done["warnings"])

    # same thesis: context not sent again; a new thesis: sent again
    [e async for e in eng.run_turn(cid, TurnRequest(text="again", tier="normal", screen={"symbol": "AAPL"}))]
    assert not client.calls[2]["messages"][-1]["content"][0]["text"].startswith("<context>")
    store.save("AAPL", {**data, "summary": "Changed view."}, "user")
    [e async for e in eng.run_turn(cid, TurnRequest(text="and now", tier="normal", screen={"symbol": "AAPL"}))]
    last = client.calls[3]["messages"][-1]["content"][0]["text"]
    assert last.startswith("<context>") and "Changed view." in last
