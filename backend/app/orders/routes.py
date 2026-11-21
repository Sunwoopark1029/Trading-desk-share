"""/api/orders/*: settings and drafts. Phase 4 part 2: NO send route exists here.

POST   /api/orders/drafts          create a draft from the user's exact inputs (validated in code)
GET    /api/orders/drafts?symbol=  active drafts (+ dashed chart lines)
DELETE /api/orders/drafts/{id}     discard
GET    /api/orders/settings       price band %, draft expiry (set on the Settings page)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.orders.context import build_context
from app.orders.drafts import DraftStore, SettingsStore, draft_lines
from app.orders.manage import TARGET_TYPES, find_target, summarize_action, validate_action
from app.orders.submit import OrderSender
from app.services import InputError, Services, jsonable
from app.timeutil import KST

router = APIRouter(prefix="/api/orders")
SOURCES = ("panel", "scenario", "chat")


def _svc(request: Request) -> Services:
    return request.app.state.services


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def stores(svc: Services) -> tuple[DraftStore, SettingsStore]:
    if getattr(svc, "drafts", None) is None:
        svc.drafts = DraftStore(clock=svc.clock)
        svc.order_settings = SettingsStore(svc.config.orders)
    if getattr(svc, "sender", None) is None:
        svc.sender = OrderSender(svc, svc.drafts, dry_run=svc.config.orders.dry_run)
    return svc.drafts, svc.order_settings


@router.get("/settings")
async def get_settings(request: Request) -> JSONResponse:
    _d, st = stores(_svc(request))
    s = st.get()
    return JSONResponse(jsonable({"price_band_pct": s.price_band_pct, "draft_ttl_seconds": s.draft_ttl_seconds,
                                  "dry_run": _svc(request).config.orders.dry_run}))


@router.post("/drafts")
async def create_draft(request: Request) -> JSONResponse:
    from app.main import UnsupportedMediaType, _body

    svc = _svc(request)
    drafts, st = stores(svc)
    try:
        body = await _body(request)
    except UnsupportedMediaType:
        return _fail(415, "send the body as application/json")
    except InputError as exc:
        return _fail(400, str(exc))
    if not isinstance(body, dict):
        return _fail(400, "body must be an object")
    source = body.get("source") if body.get("source") in SOURCES else "panel"
    if body.get("action") in ("modify", "cancel"):
        return await _action_draft(svc, body, source)
    try:
        symbol = svc.validate_symbol(str(body.get("symbol") or ""))
    except InputError as exc:
        return _fail(400, str(exc))
    # keep the user's values as strings: numbers are never re-typed or rounded
    inputs = {k: (str(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v)
              for k, v in body.items() if k != "source"}
    inputs["symbol"] = symbol
    settings = st.get()
    ctx = await build_context(svc, symbol, settings.price_band_pct)
    d = drafts.create(inputs, ctx, ttl=settings.draft_ttl_seconds, source=source,
                      dry_run=svc.config.orders.dry_run)
    return JSONResponse(jsonable(d.view(svc.clock())), status_code=201)


async def _action_draft(svc: Services, body: dict[str, Any], source: str) -> JSONResponse:
    """Modify / cancel draft for an open order (Toss or DRY RUN simulated)."""
    drafts, st = stores(svc)
    ttype, tid = body.get("target_type"), body.get("target_id")
    if ttype not in TARGET_TYPES or not isinstance(tid, str) or not tid:
        return _fail(400, "target_type and target_id are required")
    if ttype != "simulated" and svc.account is not None:
        await svc.account.refresh_now()          # act on the latest state of the order
    target = find_target(svc, ttype, tid)
    inputs = {k: (str(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v)
              for k, v in body.items() if k != "source"}
    settings = st.get()
    if target is None:   # gone (filled / cancelled meanwhile) or wrong id: no draft is created
        return JSONResponse({"status": "invalid", "errors": [{"code": "target_not_found", "params": {}}]},
                            status_code=201)
    ctx = await build_context(svc, target["symbol"], settings.price_band_pct)
    res = validate_action(inputs, target, ctx)
    en, ko = summarize_action(res.normalized)
    d = drafts.add(inputs, res, en, ko, ctx, ttl=settings.draft_ttl_seconds, source=source,
                   dry_run=svc.config.orders.dry_run)
    return JSONResponse(jsonable(d.view(svc.clock())), status_code=201)


@router.get("/drafts")
async def list_drafts(request: Request, symbol: str = "") -> JSONResponse:
    svc = _svc(request)
    drafts, _st = stores(svc)
    sym = symbol.strip().upper() or None
    active = drafts.active(sym)
    now_iso = datetime.fromtimestamp(svc.clock(), UTC).astimezone(KST).isoformat()
    done = [d.view(svc.clock()) | {"result": d.meta.get("result")} for d in drafts.done(sym)]
    from app.orders.chart_marks import fill_marks, order_lines

    lines = draft_lines(active, now_iso) + [l for l in order_lines(svc, now_iso) if sym is None or l["symbol"] == sym]
    rules = getattr(svc, "rules", None)
    if rules is not None:            # armed automatic rules: dashed lines at their touch / trigger levels
        lines += [l for l in rules.chart_lines(now_iso) if sym is None or l["symbol"] == sym]
    fills = [f for f in fill_marks(svc) if sym is None or f["symbol"] == sym]
    return JSONResponse(jsonable({"drafts": [d.view(svc.clock()) for d in active], "done": done,
                                  "overlays": lines, "fills": fills, "dry_run": svc.config.orders.dry_run}))


def remote_block(request: Request, svc: Any, body: dict[str, Any]) -> JSONResponse | None:
    """Phase 5: sending from the phone is off unless remote.orders_enabled; then it needs the PIN and
    (by default) limit orders only. The home PC is unchanged."""
    from app.auth import AuthError

    if not getattr(request.state, "remote", False):
        return None
    from app.auth import request_trusted
    if request_trusted(request):          # trusted browser: home-PC rules (cloud plan, 2026-11-21)
        return None
    auth = request.app.state.auth_holder.get("auth")
    if auth is None or not auth.cfg.orders_enabled:
        return JSONResponse({"ok": False, "status": "error", "error": "remote_orders_off"}, status_code=403)
    d = svc.drafts.get(str(body.get("draft_id") or "")) if getattr(svc, "drafts", None) else None
    if d is not None and auth.cfg.block_market_orders and \
            str((d.result.normalized or {}).get("order_type") or "").upper() == "MARKET":
        return JSONResponse({"ok": False, "status": "error", "error": "remote_market_blocked"}, status_code=403)
    try:
        auth.verify_pin(body.get("pin"), getattr(request.state, "who", None))
    except AuthError as e:
        return JSONResponse({"ok": False, "status": "error", "error": e.code, **e.params}, status_code=e.status)
    return None


@router.post("/submit")
async def submit(request: Request) -> JSONResponse:
    """Send ONE approved draft (the only send route). DRY RUN: simulated, Toss is not called."""
    from app.main import UnsupportedMediaType, _body

    svc = _svc(request)
    stores(svc)
    try:
        body = await _body(request)
    except UnsupportedMediaType:
        return _fail(415, "send the body as application/json")
    except InputError as exc:
        return _fail(400, str(exc))
    if not isinstance(body, dict) or not isinstance(body.get("draft_id"), str) or body.get("approved") is not True:
        return _fail(400, "send {draft_id, approved: true} from the approval screen")
    blocked = remote_block(request, svc, body)
    if blocked is not None:
        return blocked
    result = await svc.sender.submit(body["draft_id"])
    return JSONResponse(jsonable(result), status_code=200 if result.get("ok") else 409)


@router.get("/journal")
async def journal(request: Request) -> JSONResponse:
    """Trade journal (read only): trades made with this program."""
    svc = _svc(request)
    stores(svc)
    return JSONResponse(await svc.journal_view())


@router.get("/simulated")
async def simulated(request: Request) -> JSONResponse:
    svc = _svc(request)
    stores(svc)
    return JSONResponse(jsonable({"dry_run": svc.config.orders.dry_run, "orders": svc.sender.simulated[:50]}))


@router.delete("/drafts/{draft_id}")
async def discard_draft(request: Request, draft_id: str) -> JSONResponse:
    drafts, _st = stores(_svc(request))
    if not drafts.discard(draft_id):
        return _fail(404, "draft not found")
    return JSONResponse({"ok": True})

