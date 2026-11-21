"""/api/rules/*: automatic rules.

GET    /api/rules                 all rules (+ engine state)
POST   /api/rules                 write a rule (stored as "proposed": NOT armed)
GET    /api/rules/{id}/check      what would block arming right now (+ warnings), for the arm screen
POST   /api/rules/{id}/arm        {approved: true[, pin]}: the USER arms it (the only way a rule is armed)
POST   /api/rules/{id}/disarm
POST   /api/rules/disarm-all      kill switch
DELETE /api/rules/{id}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.rules.engine import RuleEngine, RuleError, _issues
from app.services import InputError, Services, jsonable

router = APIRouter(prefix="/api/rules")


def _svc(request: Request) -> Services:
    return request.app.state.services


def engine(svc: Services) -> RuleEngine:
    if getattr(svc, "rules", None) is None:
        svc.rules = RuleEngine(svc)  # type: ignore[attr-defined]
    return svc.rules  # type: ignore[attr-defined]


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _err(e: RuleError, status: int = 409) -> JSONResponse:
    return JSONResponse({"ok": False, "error": e.code, "params": e.params, "issues": e.issues}, status_code=status)


async def _json(request: Request) -> dict[str, Any] | JSONResponse:
    from app.main import UnsupportedMediaType, _body
    try:
        body = await _body(request)
    except UnsupportedMediaType:
        return _fail(415, "send the body as application/json")
    except InputError as exc:
        return _fail(400, str(exc))
    return body if isinstance(body, dict) else _fail(400, "body must be an object")


def remote_arm_block(request: Request, rule: Any, body: dict[str, Any]) -> JSONResponse | None:
    """From the phone, arming follows the phone order rules: switched on, PIN, limit only by default."""
    from app.auth import AuthError

    if not getattr(request.state, "remote", False):
        return None
    from app.auth import request_trusted
    if request_trusted(request):          # trusted browser: home-PC rules (cloud plan, 2026-11-21)
        return None
    auth = request.app.state.auth_holder.get("auth")
    if auth is None or not auth.cfg.orders_enabled:
        return JSONResponse({"ok": False, "error": "remote_orders_off"}, status_code=403)
    if auth.cfg.block_market_orders and str(rule.order.get("order_type")).upper() == "MARKET":
        return JSONResponse({"ok": False, "error": "remote_market_blocked"}, status_code=403)
    try:
        auth.verify_pin(body.get("pin"), getattr(request.state, "who", None))
    except AuthError as e:
        return JSONResponse({"ok": False, "error": e.code, **e.params}, status_code=e.status)
    return None


@router.get("")
async def list_rules(request: Request) -> JSONResponse:
    return JSONResponse(jsonable(engine(_svc(request)).list_view()))


@router.post("")
async def create_rule(request: Request) -> JSONResponse:
    body = await _json(request)
    if isinstance(body, JSONResponse):
        return body
    svc = _svc(request)
    try:
        body["symbol"] = svc.validate_symbol(str(body.get("symbol") or ""))
    except InputError as exc:
        return _fail(400, str(exc))
    eng = engine(svc)
    rule, errors, warnings = await eng.create(body, "user")
    out = {"ok": rule is not None, "rule": None if rule is None else eng.view(rule),
           "errors": _issues(errors), "warnings": _issues(warnings)}
    return JSONResponse(jsonable(out), status_code=201 if rule is not None else 422)


@router.get("/{rule_id}/check")
async def check_rule(request: Request, rule_id: str) -> JSONResponse:
    eng = engine(_svc(request))
    r = eng.rules.get(rule_id)
    if r is None:
        return _fail(404, "rule not found")
    errors, warnings = await eng.check_now(r)
    return JSONResponse(jsonable({"rule": eng.view(r), "errors": _issues(errors), "warnings": _issues(warnings),
                                  "dry_run": eng.dry_run}))


@router.post("/{rule_id}/arm")
async def arm_rule(request: Request, rule_id: str) -> JSONResponse:
    body = await _json(request)
    if isinstance(body, JSONResponse):
        return body
    if body.get("approved") is not True:
        return _fail(400, "send {approved: true} from the arm screen")
    eng = engine(_svc(request))
    r = eng.rules.get(rule_id)
    if r is None:
        return _fail(404, "rule not found")
    blocked = remote_arm_block(request, r, body)
    if blocked is not None:
        return blocked
    try:
        r = await eng.arm(rule_id)
    except RuleError as e:
        return _err(e)
    return JSONResponse(jsonable({"ok": True, "rule": eng.view(r)}))


@router.post("/disarm-all")
async def disarm_all(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "disarmed": engine(_svc(request)).disarm_all()})


@router.post("/{rule_id}/disarm")
async def disarm_rule(request: Request, rule_id: str) -> JSONResponse:
    eng = engine(_svc(request))
    try:
        r = eng.disarm(rule_id)
    except RuleError as e:
        return _err(e, 404)
    return JSONResponse(jsonable({"ok": True, "rule": eng.view(r)}))


@router.delete("/{rule_id}")
async def delete_rule(request: Request, rule_id: str) -> JSONResponse:
    try:
        engine(_svc(request)).delete(rule_id)
    except RuleError as e:
        return _err(e, 404 if e.code == "rule_not_found" else 409)
    return JSONResponse({"ok": True})
