"""/api/chat/* and /api/usage/summary. Answers stream as Server-Sent Events."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.chat.engine import KEY_NOT_SET, ChatEngine, TurnRequest
from app.chat.store import KINDS
from app.chat.usage import budget_decision, budget_status
from app.services import InputError, jsonable
from app.store import SYMBOL_RE

log = logging.getLogger("desk.chat")
router = APIRouter(prefix="/api")


def _engine(request: Request) -> ChatEngine | None:
    return getattr(request.app.state, "chat", None)


def _fail(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": message, **extra}, status_code=status)


def _budget(eng: ChatEngine) -> dict[str, Any]:
    today, month, _all = eng.usage.totals(eng.clock())
    st = budget_status(today.usd, month.usd, eng.svc.config.budget)
    return {"state": st.state, "day_usd": st.day_usd, "month_usd": st.month_usd,
            "daily_limit_usd": st.daily_limit, "monthly_limit_usd": st.monthly_limit,
            "day_ratio": st.day_ratio, "month_ratio": st.month_ratio,
            "warn_at": eng.svc.config.budget.warn_at}


@router.get("/chat/status")
async def chat_status(request: Request) -> JSONResponse:
    eng = _engine(request)
    if eng is None:
        return JSONResponse({"enabled": False, "message": "no llm section in config.yaml"})
    return JSONResponse(jsonable({
        "enabled": True,
        "key": "set" if eng.key_set else "empty",
        "message": None if eng.key_set else KEY_NOT_SET,
        "tiers": [{"name": n, "label": t.label, "model": t.model}
                  for n, t in eng.llm.tiers.items() if n != "background"],
        "web_search_default": eng.llm.web_search.default_on,
        "default_tier": getattr(getattr(eng.svc, "user_settings", None), "ai", None) and eng.svc.user_settings.ai.default_tier,
        "candles_mode": eng.llm.candles.mode,
        "quick_prompt": getattr(getattr(getattr(eng.svc, "user_settings", None), "ai", None), "quick_prompt", ""),
        "budget": _budget(eng),
    }))


@router.get("/usage/summary")
async def usage_summary(request: Request) -> JSONResponse:
    eng = _engine(request)
    if eng is None:
        return _fail(404, "chat not configured")
    today, month, total = eng.usage.totals(eng.clock())
    mid, fx_time, _ = eng.svc.fx_rate()

    def block(t: Any) -> dict[str, Any]:
        krw, est = t.krw_total(mid)
        share = {m: (v / t.usd if t.usd else Decimal(0)) for m, v in sorted(t.by_model.items())}
        return {"usd": t.usd, "krw": krw, "krw_estimated": est, "calls": t.calls,
                "by_model_usd": dict(sorted(t.by_model.items())), "by_model_share": share}

    return JSONResponse(jsonable({
        "today": block(today), "month": block(month), "all": block(total),
        "budget": _budget(eng), "fx": {"rate": mid, "time": fx_time, "source": "Toss mid (reference)"},
    }))


def _symbol_kind(eng: ChatEngine, symbol: Any, kind: Any) -> tuple[str | None, str | None]:
    """Optional ticker (format only: works before the Toss stock list has loaded; a question is still checked
    against the list when sent) + chat kind. Raises InputError."""
    sym = None
    if symbol not in (None, ""):
        raw = str(symbol)
        if len(raw) > 20 or not SYMBOL_RE.match(raw):
            raise InputError("ticker must be letters, digits, '.' or '-'")
        sym = raw.upper()
    if kind not in (None, "") and kind not in KINDS:
        raise InputError(f"kind must be one of {', '.join(KINDS)}")
    return sym, (kind or None)


@router.get("/chat/conversations")
async def list_conversations(request: Request, symbol: str | None = None, kind: str | None = None) -> JSONResponse:
    """All chats, or only one ticker's chats of one kind (D5: each ticker has its own threads)."""
    eng = _engine(request)
    if eng is None:
        return _fail(404, "chat not configured")
    try:
        sym, k = _symbol_kind(eng, symbol, kind)
    except InputError as exc:
        return _fail(400, str(exc))
    return JSONResponse({"conversations": eng.store.list(sym, k)})


@router.post("/chat/conversations")
async def create_conversation(request: Request) -> JSONResponse:
    """New chat; optional JSON body {symbol, kind} (kind desk | research, default desk)."""
    eng = _engine(request)
    if eng is None:
        return _fail(404, "chat not configured")
    body: Any = {}
    if (await request.body()).strip():
        try:
            body = await request.json()
        except ValueError:
            return _fail(400, "body must be JSON")
    if not isinstance(body, dict):
        return _fail(400, "body must be an object")
    try:
        sym, k = _symbol_kind(eng, body.get("symbol"), body.get("kind"))
    except InputError as exc:
        return _fail(400, str(exc))
    return JSONResponse({"id": eng.store.create(symbol=sym, kind=k or "desk")})


@router.get("/chat/conversations/{cid}")
async def get_conversation(request: Request, cid: str) -> JSONResponse:
    eng = _engine(request)
    if eng is None:
        return _fail(404, "chat not configured")
    conv = eng.store.get(cid)
    if conv is None:
        return _fail(404, "chat not found")
    items = [{"id": r["id"], "active": bool(r["active"]), **r["display"]}
             for r in eng.store.rows(cid) if r.get("display")]
    return JSONResponse({"id": cid, "title": conv["title"], "summarized": bool(conv.get("summary")),
                         "symbol": conv.get("symbol"), "kind": conv.get("kind") or "desk", "messages": items})


@router.delete("/chat/conversations/{cid}")
async def delete_conversation(request: Request, cid: str) -> JSONResponse:
    eng = _engine(request)
    if eng is None:
        return _fail(404, "chat not configured")
    if not eng.store.delete(cid):
        return _fail(404, "chat not found")
    return JSONResponse({"ok": True})


def _sse(ev: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(jsonable(ev), ensure_ascii=False)}\n\n".encode()


@router.post("/chat/conversations/{cid}/messages")
async def send_message(request: Request, cid: str) -> Any:
    from app.main import UnsupportedMediaType, _body  # shared JSON-body rules (415 for non-JSON)

    eng = _engine(request)
    if eng is None:
        return _fail(404, "chat not configured")
    if not eng.key_set:
        return _fail(409, KEY_NOT_SET)
    try:
        body = await _body(request)
    except UnsupportedMediaType:
        return _fail(415, "send the body as application/json")
    except InputError as exc:
        return _fail(400, str(exc))
    if not isinstance(body, dict):
        return _fail(400, "body must be an object")
    text = str(body.get("text") or "").strip()
    tier = body.get("tier") or "normal"
    if not text:
        return _fail(400, "message is empty")
    if len(text) > 8000:
        return _fail(400, "message is too long (max 8000 characters)")
    if tier not in ("normal", "deep", "very_deep"):
        return _fail(400, "tier must be normal, deep or very_deep")
    conv = eng.store.get(cid)
    if conv is None:
        return _fail(404, "chat not found")
    screen = body.get("screen") if isinstance(body.get("screen"), dict) else {}
    try:
        sym = eng.svc.validate_symbol(str(screen.get("symbol") or conv.get("symbol") or eng.svc.config.tickers.default))
    except InputError as exc:
        return _fail(400, str(exc))
    if conv.get("symbol") and conv["symbol"] != sym:          # D5: one ticker per chat
        return _fail(409, f"This chat is about {conv['symbol']}. Open {sym}'s chat (or start a new one) to ask about {sym}.",
                     chat_symbol=conv["symbol"])

    b = _budget(eng)
    st = budget_status(Decimal(str(b["day_usd"])), Decimal(str(b["month_usd"])), eng.svc.config.budget)
    decision = budget_decision(st, tier)
    if decision == "block":
        return _fail(403, "Budget limit reached: Deep and Very deep are blocked until the limit resets.",
                     budget=jsonable(b))
    if decision == "confirm" and body.get("confirm_over_budget") is not True:
        return _fail(409, "Budget limit reached. Send anyway on Normal?", needs_confirm=True, budget=jsonable(b))

    if not conv.get("symbol"):
        eng.store.set_symbol(cid, sym)                          # a new chat takes the ticker of its first question
    lang = body.get("lang") if body.get("lang") in ("en", "ko") else "en"
    req = TurnRequest(text=text, tier=tier, screen=screen, web_search=body.get("web_search") is True, lang=lang)

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for ev in eng.run_turn(cid, req):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001 - report without leaking details
            yield _sse({"type": "error", "message": safe_error(exc)})
        yield _sse({"type": "end"})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def safe_error(exc: BaseException) -> str:
    """A message for the screen without keys, URLs or request bodies."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        anthropic = None  # type: ignore[assignment]
    name = type(exc).__name__
    if anthropic is not None:
        if isinstance(exc, anthropic.AuthenticationError):
            return "Anthropic rejected the API key (check ANTHROPIC_API_KEY in .env)"
        if isinstance(exc, anthropic.PermissionDeniedError):
            return "Anthropic: this key has no access to that model"
        if isinstance(exc, anthropic.NotFoundError):
            return "Anthropic: model not found (check the model IDs in config.yaml with check_models.cmd)"
        if isinstance(exc, anthropic.RateLimitError):
            return "Anthropic rate limit or no credits left; wait and retry (check Console billing)"
        if isinstance(exc, anthropic.BadRequestError):
            detail = str(getattr(exc, "message", "") or "")[:300]  # API's own text: holds no key
            log.warning("Anthropic 400: %s", detail)
            return f"Anthropic refused the request (400): {detail}"
        if isinstance(exc, anthropic.APIStatusError):
            return f"Anthropic error {exc.status_code}; try again"
        if isinstance(exc, anthropic.APIConnectionError):
            return "Could not reach Anthropic (network)"
    if isinstance(exc, InputError):
        return str(exc)
    log.error("chat turn failed: %s", name)
    return f"chat failed ({name})"
