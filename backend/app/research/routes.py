"""Company view routes (phase 6 part B). Read-only; sources are called only when a company is opened.

GET /api/research/{symbol}/overview                          basics, profile, next earnings, price, sector, statuses
GET /api/research/{symbol}/financials?freq=annual|quarterly&years=   statement tables
GET /api/research/{symbol}/metrics                           the fundamentals pack (every value with id + display)
GET /api/research/{symbol}/earnings                          EPS surprises, calendar, SEC EPS history
GET /api/research/{symbol}/analysts                          rating trends, yfinance targets (unofficial), price
GET /api/research/{symbol}/peers                             top N in the company's Toss sector, medians
GET /api/research/{symbol}/news?days=                        Finnhub company news, longer window than the Desk
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.research.service import research_of
from app.services import InputError, Services, jsonable

router = APIRouter(prefix="/api/research")


def _svc(request: Request) -> Services:
    return request.app.state.services


async def _run(coro: Any) -> JSONResponse:
    try:
        return JSONResponse(jsonable(await coro))
    except InputError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@router.get("/{symbol}/overview")
async def overview(request: Request, symbol: str) -> JSONResponse:
    return await _run(research_of(_svc(request)).overview(symbol))


@router.get("/{symbol}/financials")
async def financials(request: Request, symbol: str, freq: str = "annual", years: int | None = None) -> JSONResponse:
    return await _run(research_of(_svc(request)).financials(symbol, freq, years))


@router.get("/{symbol}/metrics")
async def metrics(request: Request, symbol: str) -> JSONResponse:
    return await _run(research_of(_svc(request)).metrics(symbol))


@router.get("/{symbol}/earnings")
async def earnings(request: Request, symbol: str) -> JSONResponse:
    return await _run(research_of(_svc(request)).earnings(symbol))


@router.get("/{symbol}/analysts")
async def analysts(request: Request, symbol: str) -> JSONResponse:
    return await _run(research_of(_svc(request)).analysts(symbol))


@router.get("/{symbol}/peers")
async def peers(request: Request, symbol: str) -> JSONResponse:
    return await _run(research_of(_svc(request)).peers(symbol))


@router.get("/{symbol}/news")
async def news(request: Request, symbol: str, days: int | None = None) -> JSONResponse:
    return await _run(research_of(_svc(request)).news(symbol, days))


# ── thesis + notes (phase 6 part C, step C4) ───────────────────────────
# GET    /api/research/{symbol}/thesis      current thesis + facts code knows now + history + notes
# PUT    /api/research/{symbol}/thesis      save from a valuation card {conversation_id, message_id, card_index,
#                                           summary?, show_on_chart?} or edit the current one {summary?, show_on_chart?}
# DELETE /api/research/{symbol}/thesis      no current thesis (history kept)
# GET / POST /api/research/{symbol}/notes   list / add {text}; DELETE /api/research/{symbol}/notes/{id}

def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


async def _json(request: Request) -> Any:
    from app.main import UnsupportedMediaType, _body
    try:
        body = await _body(request)
    except UnsupportedMediaType:
        raise InputError("send the body as application/json") from None
    if not isinstance(body, dict):
        raise InputError("body must be an object")
    return body


def _flags(raw: Any) -> dict[str, bool] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, bool) for k, v in raw.items()):
        raise InputError("show_on_chart must map reference ids to true / false")
    return raw


def _card_from_chat(request: Request, symbol: str, body: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    """The valuation card exactly as the chat saved it (never numbers from the request)."""
    eng = getattr(request.app.state, "chat", None)
    if eng is None:
        raise InputError("chat not configured")
    cid = str(body.get("conversation_id") or "")
    conv = eng.store.get(cid) if cid else None
    if conv is None:
        raise InputError("chat not found")
    mid, idx = body.get("message_id"), body.get("card_index", 0)
    if isinstance(mid, bool) or not isinstance(mid, int) or isinstance(idx, bool) or not isinstance(idx, int):
        raise InputError("message_id and card_index must be whole numbers")
    row = next((r for r in eng.store.rows(cid) if r["id"] == mid), None)
    cards = ((row or {}).get("display") or {}).get("valuations") or []
    if not 0 <= idx < len(cards):
        raise InputError("valuation card not found in that chat")
    card = cards[idx]
    if str(card.get("symbol") or "").upper() != symbol:
        raise InputError(f"that card is about {card.get('symbol')}, not {symbol}")
    # the turn's number-check warnings stay with the thesis, so the user sees them on the thesis card
    warns = [w for w in ((row or {}).get("display") or {}).get("warnings") or [] if "nverified number" in w]
    return card, cid, warns


@router.get("/{symbol}/thesis")
async def get_thesis(request: Request, symbol: str) -> JSONResponse:
    from app.research.thesis import thesis_view
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    return JSONResponse(await thesis_view(svc, sym))


@router.put("/{symbol}/thesis")
async def put_thesis(request: Request, symbol: str) -> JSONResponse:
    from app.research.store import research_store_of
    from app.research.thesis import edited, save_card, thesis_view
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
        body = await _json(request)
        flags = _flags(body.get("show_on_chart"))
        summary = body.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise InputError("summary must be text")
        if body.get("conversation_id") is not None:
            card, cid, warns = _card_from_chat(request, sym, body)
            await save_card(svc, card, saved_by="user", summary=summary, show_on_chart=flags, conversation_id=cid,
                            warnings=warns)
        else:
            store = research_store_of(svc)
            cur = store.current(sym)
            if cur is None:
                raise InputError("no thesis saved for this ticker yet")
            store.save(sym, edited(cur, summary, flags), cur["saved_by"])    # who saved it stays; edit is marked
    except (InputError, ValueError) as exc:
        return _fail(400, str(exc))
    return JSONResponse(await thesis_view(svc, sym))


@router.delete("/{symbol}/thesis")
async def delete_thesis(request: Request, symbol: str) -> JSONResponse:
    from app.research.store import research_store_of
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    if not research_store_of(svc).clear(sym):
        return _fail(404, "no current thesis")
    return JSONResponse({"ok": True})


@router.get("/{symbol}/notes")
async def get_notes(request: Request, symbol: str) -> JSONResponse:
    from app.research.store import research_store_of
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    return JSONResponse({"symbol": sym, "notes": research_store_of(svc).notes(sym)})


@router.post("/{symbol}/notes")
async def add_note(request: Request, symbol: str) -> JSONResponse:
    from app.research.store import research_store_of
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
        body = await _json(request)
        source = body.get("source", "user")
        if source not in ("user", "pinned"):
            raise InputError("source must be user or pinned")
        note = research_store_of(svc).add_note(sym, str(body.get("text") or ""), source)
    except (InputError, ValueError) as exc:
        return _fail(400, str(exc))
    return JSONResponse({"note": note})


@router.delete("/{symbol}/notes/{note_id}")
async def delete_note(request: Request, symbol: str, note_id: int) -> JSONResponse:
    from app.research.store import research_store_of
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
    except InputError as exc:
        return _fail(400, str(exc))
    if not research_store_of(svc).delete_note(sym, note_id):
        return _fail(404, "note not found")
    return JSONResponse({"ok": True})
