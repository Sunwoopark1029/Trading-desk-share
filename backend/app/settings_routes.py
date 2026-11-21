"""/api/settings: the Settings page. GET effective values (+ defaults), PUT changes, POST reset."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services import InputError, Services, jsonable
from app.user_settings import SettingsError, SettingsFile, UserSettings, apply, defaults_from, merge

router = APIRouter(prefix="/api")


def _svc(request: Request) -> Services:
    return request.app.state.services


def init(svc: Services, file: SettingsFile | None = None) -> UserSettings:
    """At startup: remember config.yaml defaults, load the saved file, apply it."""
    svc.settings_file = file or SettingsFile()
    svc.settings_defaults = defaults_from(svc.config)
    svc.user_settings = svc.settings_file.load(svc.settings_defaults)
    apply(svc.user_settings, svc.config)
    return svc.user_settings


def current(svc: Services) -> UserSettings:
    if getattr(svc, "user_settings", None) is None:
        init(svc)
    return svc.user_settings


def check_tickers(svc: Services, s: UserSettings) -> None:
    """Tickers must exist in the Toss US list (once it is loaded)."""
    if not svc.stocks:
        return
    names = {s.tickers.default, *s.tickers.watchlist, *s.tickers.related,
             *(t for lst in s.tickers.related.values() for t in lst)}
    missing = sorted(t for t in names if t not in svc.stocks)
    if missing:
        raise SettingsError(f"not in the Toss US stock list: {', '.join(missing)}")


def view(svc: Services) -> dict[str, Any]:
    llm = svc.config.llm
    return jsonable({
        "settings": current(svc).model_dump(mode="json"),
        "defaults": svc.settings_defaults.model_dump(mode="json"),
        "read_only": {"dry_run": svc.config.orders.dry_run,
                      "tiers": [] if llm is None else [{"name": n, "label": t.label, "model": t.model}
                                                       for n, t in llm.tiers.items() if n != "background"]},
    })


@router.get("/settings")
async def get_settings(request: Request) -> JSONResponse:
    return JSONResponse(view(_svc(request)))


@router.put("/settings")
async def put_settings(request: Request) -> JSONResponse:
    from app.main import UnsupportedMediaType, _body

    svc = _svc(request)
    try:
        body = await _body(request)
        new = merge(current(svc), body)
        check_tickers(svc, new)
    except UnsupportedMediaType:
        return JSONResponse({"detail": "send the body as application/json"}, status_code=415)
    except (InputError, SettingsError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    svc.settings_file.save(new)
    svc.user_settings = new
    apply(new, svc.config)
    return JSONResponse(view(svc))


@router.post("/settings/reset")
async def reset_settings(request: Request) -> JSONResponse:
    svc = _svc(request)
    current(svc)
    svc.user_settings = svc.settings_defaults
    svc.settings_file.save(svc.user_settings)
    apply(svc.user_settings, svc.config)
    return JSONResponse(view(svc))
