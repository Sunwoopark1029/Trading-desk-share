"""FastAPI app: read-only dashboard API under /api. No order code.

Run (start.cmd does this): uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
Set DESK_DISABLE_POLLERS=1 to start without background polling (no network).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import overlays as overlay_mod
from app import pnl as pnl_mod
from app import store
from app.chat.engine import ChatEngine
from app.chat.routes import router as chat_router
from app.explore import routes as explore_routes
from app import watchlist_routes
from app.research import routes as research_routes
from app.chat.store import ChatStore
from app.chat.usage import UsageLog
from app import market_routes
from app.orders import routes as order_routes
from app.rules import routes as rules_routes
from app import settings_routes
from app.services import InputError, Services, jsonable
from app.auth import COOKIE, DEVICE_COOKIE, PUBLIC_API, Auth, AuthError, cookie_token, request_origin
from app.settings import REPO_ROOT

log = logging.getLogger("desk")
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "*.ts.net"]   # *.ts.net: your Tailscale name (serve, phase 5)
TS_ORIGIN = re.compile(r"^https://[a-z0-9-]+\.[a-z0-9-]+\.ts\.net$")
SCREEN_DIR = REPO_ROOT / "frontend" / "dist"
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173",
                             "http://127.0.0.1:8000", "http://localhost:8000"})
WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
router = APIRouter(prefix="/api")


def _svc(request: Request) -> Services:
    return request.app.state.services


def _ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(jsonable(data), status_code=status)


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


async def _body(request: Request) -> Any:
    """Request JSON with numbers as exact Decimal (prices saved as entered).

    Content-Type must be application/json, so a cross-site "simple" form/text POST
    (sent by the browser without a CORS check) is refused.
    """
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise UnsupportedMediaType()
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8-sig") or "null", parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InputError("request body is not valid JSON") from None


class UnsupportedMediaType(Exception):
    """Request body is not sent as application/json -> HTTP 415."""


# ── health ───────────────────────────────────────────────────────────────

@router.get("/health")
async def health(request: Request) -> JSONResponse:
    return _ok(_svc(request).health())


@router.get("/ui-config")
async def ui_config(request: Request) -> JSONResponse:
    """Non-secret settings the screen needs (tickers, poll intervals) from config.yaml."""
    svc = _svc(request)
    cfg = svc.config
    p = cfg.polling
    us = settings_routes.current(svc)
    return _ok({
        "tickers": {"default": cfg.tickers.default.upper(),
                    "compare": [s.upper() for s in cfg.tickers.compare]},
        "user": {"general": us.general.model_dump(mode="json"), "chart": us.chart.model_dump(mode="json"),
                 "ai": {"default_tier": us.ai.default_tier}},
        "polling": {"quotes_seconds": p.quotes_seconds, "candles_seconds": p.candles_seconds,
                    "chart_seconds": p.chart_seconds,
                    "fx_seconds": p.fx_seconds, "yfinance_seconds": p.yfinance_seconds},
    })


@router.post("/health/toss/retry")
async def toss_retry(request: Request) -> JSONResponse:
    svc = _svc(request)
    if not svc.reset_toss():
        return _fail(409, "Toss keys not set in .env")
    return _ok({"ok": True, "message": "Toss will try again on the next poll"})


# ── market data ──────────────────────────────────────────────────────────

@router.get("/tickers")
async def tickers(request: Request, q: str = "") -> JSONResponse:
    return _ok(_svc(request).search_tickers(q))


@router.get("/candles")
async def candles(request: Request, symbol: str = "", tf: str = "5m") -> JSONResponse:
    svc = _svc(request)
    try:
        sym = svc.validate_symbol(symbol)
        return _ok(await svc.candles_view(sym, tf))
    except InputError as exc:
        return _fail(400, str(exc))


@router.get("/strip")
async def strip(request: Request) -> JSONResponse:
    return _ok(_svc(request).strip_view())


# ── positions / P&L ──────────────────────────────────────────────────────

@router.get("/positions")
async def positions(request: Request) -> JSONResponse:
    try:
        return _ok(_svc(request).positions_view())
    except store.StoreError as exc:
        return _fail(422, str(exc))


@router.put("/positions/plans")
async def put_plans(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        plans = body.get("plans") if isinstance(body, dict) else body
        saved = _svc(request).save_plans(plans)
    except UnsupportedMediaType:
        return _fail(415, "send the body as application/json")
    except InputError as exc:
        return _fail(400, str(exc))
    except store.StoreError as exc:  # broken positions.json: never overwritten
        return _fail(409, str(exc))
    return _ok({"plans": saved})


@router.get("/pnl")
async def pnl(request: Request, period: str = "all") -> JSONResponse:
    try:
        return _ok(_svc(request).pnl_view(period))
    except InputError as exc:
        return _fail(400, str(exc))
    except (store.StoreError, pnl_mod.PnlError) as exc:
        return _fail(422, str(exc))


# ── overlays ─────────────────────────────────────────────────────────────

@router.get("/overlays")
async def list_overlays(request: Request, symbol: str | None = None, timeframe: str | None = None) -> JSONResponse:
    if timeframe is not None and timeframe not in ("1m", "5m", "1d"):
        return _fail(400, "invalid timeframe")
    if symbol is not None:
        symbol = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol):
            return _fail(400, "invalid symbol")
    try:
        return _ok({"overlays": _svc(request).list_overlays(symbol, timeframe)})
    except store.StoreError as exc:
        return _fail(422, str(exc))


@router.post("/overlays")
async def save_overlay(request: Request) -> JSONResponse:
    try:
        return _ok(_svc(request).save_overlay(await _body(request)))
    except UnsupportedMediaType:
        return _fail(415, "send the body as application/json")
    except (InputError, overlay_mod.OverlayError) as exc:
        return _fail(400, str(exc))
    except store.StoreError as exc:
        return _fail(409, str(exc))


# ── login (phase 5) ──────────────────────────────────────────────────────

def _auth(request: Request) -> Auth | None:
    return request.app.state.auth_holder.get("auth")


def _origin(request: Request) -> tuple[bool, str | None]:
    return request_origin({k.lower(): v for k, v in request.headers.raw})


@router.get("/auth/status")
async def auth_status(request: Request) -> JSONResponse:
    auth = _auth(request)
    remote, _who = _origin(request)
    if auth is None:   # tests without login
        return _ok({"required": False, "logged_in": True, "passcode_set": True, "pin_set": True, "remote": remote,
                    "remote_orders": False, "block_market": True})
    cfg = auth.cfg
    return _ok({"required": True, "logged_in": auth.check(request.cookies.get(COOKIE), remote),
                "passcode_set": auth.passcode_set(), "pin_set": auth.pin_set(), "remote": remote,
                "trusted": remote and auth.trusted(request.cookies.get(DEVICE_COOKIE), _who),
                "remote_orders": cfg.orders_enabled, "block_market": cfg.block_market_orders,
                "remote_hours": cfg.remote_session_hours, "local_days": cfg.local_session_days})


@router.post("/auth/login")
async def auth_login(request: Request) -> JSONResponse:
    auth = _auth(request)
    if auth is None:
        return _ok({"ok": True})
    body = await _body(request)
    remote, who = _origin(request)
    try:
        token, ttl = auth.login(body.get("passcode") if isinstance(body, dict) else None, remote, who,
                                trusted=remote and auth.trusted(request.cookies.get(DEVICE_COOKIE), who))
    except AuthError as e:
        return JSONResponse({"detail": e.code, "code": e.code, **e.params}, status_code=e.status)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=ttl, httponly=True, samesite="strict", secure=remote, path="/")
    return resp


@router.post("/auth/trust")
async def auth_trust(request: Request) -> JSONResponse:
    """Trust this browser (PIN once). Only for devices that come in through Tailscale."""
    auth = _auth(request)
    if auth is None:
        return _fail(400, "login is off")
    body = await _body(request)
    body = body if isinstance(body, dict) else {}
    remote, who = _origin(request)
    if not remote:
        return JSONResponse({"detail": "trust_remote_only", "code": "trust_remote_only"}, status_code=400)
    try:
        token, max_age = auth.trust_device(body.get("pin"), who, body.get("name"))
    except AuthError as e:
        return JSONResponse({"detail": e.code, "code": e.code, **e.params}, status_code=e.status)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(DEVICE_COOKIE, token, max_age=max_age, httponly=True, samesite="strict", secure=True, path="/")
    return resp


@router.get("/auth/devices")
async def auth_devices(request: Request) -> JSONResponse:
    auth = _auth(request)
    if auth is None:
        return _ok({"devices": []})
    return _ok({"devices": auth.list_devices(request.cookies.get(DEVICE_COOKIE))})


@router.delete("/auth/devices/{device_id}")
async def auth_remove_device(request: Request, device_id: str) -> JSONResponse:
    auth = _auth(request)
    if auth is None or not auth.remove_device(device_id):
        return _fail(404, "device not found")
    return _ok({"ok": True})


@router.post("/auth/devices/remove-all")
async def auth_remove_all_devices(request: Request) -> JSONResponse:
    auth = _auth(request)
    return _ok({"ok": True, "removed": auth.remove_all_devices() if auth is not None else 0})


@router.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    auth = _auth(request)
    if auth is not None:
        auth.logout(request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.delete("/overlays/{overlay_id}")
async def delete_overlay(request: Request, overlay_id: str) -> JSONResponse:
    try:
        if not _svc(request).delete_overlay(overlay_id):
            return _fail(404, "overlay not found")
    except store.StoreError as exc:
        return _fail(409, str(exc))
    return _ok({"ok": True})


# ── middleware (pure ASGI) ───────────────────────────────────────────────

class OriginCheck:
    """403 for write requests whose Origin header is not this app (cross-site POST)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in WRITE_METHODS:
            headers = dict(scope["headers"])
            origin = headers.get(b"origin")
            ok = origin is None or origin.decode("latin-1") in ALLOWED_ORIGINS or (
                bool(TS_ORIGIN.match(origin.decode("latin-1"))) and request_origin(headers)[1] is not None)
            if not ok:
                await _fail(403, "request from another site refused")(scope, receive, send)
                return
        await self.app(scope, receive, send)


class LoginGate:
    """Phase 5. Remote requests must come from your own Tailscale devices (identity header present:
    Funnel / public traffic has none). Every /api route except login/status needs a session.
    holder["auth"] is set at startup; a real run without it refuses everything (fails closed)."""

    def __init__(self, app: ASGIApp, holder: dict[str, Any]) -> None:
        self.app = app
        self.holder = holder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        remote, who = request_origin(headers)
        auth: Auth | None = self.holder.get("auth")
        if auth is None and self.holder.get("required"):
            await _fail(503, "login not ready")(scope, receive, send)
            return
        if remote and auth is not None and not auth.cfg.allow_remote:
            await _fail(403, "access from other devices is off (config remote.allow_remote)")(scope, receive, send)
            return
        if remote and who is None:
            await _fail(403, "only your own Tailscale devices can open this")(scope, receive, send)
            return
        if auth is not None and remote and auth.cfg.allowed_logins and who not in auth.cfg.allowed_logins:
            await _fail(403, "this Tailscale account is not allowed")(scope, receive, send)
            return
        scope.setdefault("state", {})
        scope["state"]["remote"], scope["state"]["who"] = remote, who
        path = scope.get("path", "")
        if auth is not None and path.startswith("/api/") and path not in PUBLIC_API:
            if not auth.check(cookie_token(headers), remote):
                await JSONResponse({"detail": "login required", "code": "login_required"},
                                   status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class CatchAll:
    """Turn any unhandled error into a 500 without re-raising it.

    Starlette's ServerErrorMiddleware re-raises after its handler, and uvicorn then prints
    the traceback with str(exc) (which could hold a URL). Here only the type name is logged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001
            log.error("unhandled %s", type(exc).__name__)  # never str(exc): may hold URLs
            if not started:
                await _fail(500, f"internal error ({type(exc).__name__})")(scope, receive, send)


# ── app ──────────────────────────────────────────────────────────────────

def _pollers_enabled() -> bool:
    return os.environ.get("DESK_DISABLE_POLLERS", "").strip().lower() not in ("1", "true", "yes")


def _make_chat(svc: Services) -> ChatEngine | None:
    """Chat engine if config.yaml has an llm section (the key may still be empty)."""
    if svc.config.llm is None:
        return None
    from app.settings import get_secrets
    key = get_secrets().ANTHROPIC_API_KEY.get_secret_value().strip()
    return ChatEngine(svc, svc.config.llm, api_key=key, store=ChatStore(), usage=UsageLog())


def create_app(services: Services | None = None, *, start_pollers: bool | None = None,
               chat: ChatEngine | None = None, auth: Auth | None = None) -> FastAPI:
    """Build the app. Tests pass their own Services (and chat engine) and start_pollers=False.
    Login: a real run always builds it from config (fails closed); tests pass auth to test it."""
    holder: dict[str, Any] = {"auth": auth, "required": services is None}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services
        if svc is None:
            from app.data_dir import migrate_to_data_dir
            from app.settings import DATA_DIR, REPO_ROOT, get_config, get_secrets
            migrate_to_data_dir(REPO_ROOT, DATA_DIR)   # old root-level data files -> data/ (once)
            svc = Services(get_config(), get_secrets())
        app.state.services = svc
        from app.watchlist import watchlist_of     # the pinned folder feeds the strip slots and news
        svc.pinned_provider = watchlist_of(svc).pinned_symbols  # type: ignore[attr-defined]
        if holder["auth"] is None and services is None:
            holder["auth"] = Auth(svc.config.remote)
        if services is None:  # real run: Settings page values override config.yaml defaults
            settings_routes.init(svc)
            if not svc.config.orders.dry_run and svc.toss is not None:
                svc.toss.enable_order_writes()   # REAL ORDERS: only because config orders.dry_run is false
                log.warning("REAL ORDERS ARE ON (config orders.dry_run: false)")
        app.state.chat = chat if chat is not None else (_make_chat(svc) if services is None else None)
        if app.state.chat is not None:
            svc.ai_credits = app.state.chat.usage  # P&L reads AI spend from logs/usage.jsonl
        if services is None:  # real run: orderbook stream, news feed, tagger (tests attach their own)
            from app.settings import get_secrets
            market_routes.build(svc, app.state.chat, get_secrets().FINNHUB_API_KEY.get_secret_value().strip())
        p3_tasks: list = []
        if start_pollers if start_pollers is not None else _pollers_enabled():
            svc.start()  # pollers only; the Toss token is issued lazily on the first call
            p3_tasks = market_routes.start(svc)
        try:
            yield
        finally:
            await market_routes.stop(svc, p3_tasks)
            await svc.stop()

    app = FastAPI(title="Personal Trading Desk", lifespan=lifespan)
    app.state.auth_holder = holder
    app.add_middleware(LoginGate, holder=holder)
    app.add_middleware(OriginCheck)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(CatchAll)  # added last = outermost of ours
    app.include_router(router)
    app.include_router(chat_router)
    app.include_router(market_routes.router)
    app.include_router(order_routes.router)
    app.include_router(rules_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(explore_routes.router)
    app.include_router(watchlist_routes.router)
    app.include_router(research_routes.router)
    if SCREEN_DIR.is_dir() and services is None:   # the built screen (start.cmd builds it), same port
        app.mount("/", StaticFiles(directory=SCREEN_DIR, html=True), name="screen")
    return app


app = create_app()
