"""Watchlist routes (phase 6 part A).

GET /api/watchlist                  folders (first run: created from the default ticker + Settings watchlist)
PUT /api/watchlist                  replace the whole document (validated; refused while the file is broken)
GET /api/watchlist/quotes?folder=   last price, change, change % for one folder's tickers (Toss)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services import InputError, Services, jsonable
from app.watchlist import WatchlistError, watchlist_of

router = APIRouter(prefix="/api/watchlist")


def _svc(request: Request) -> Services:
    return request.app.state.services


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


@router.get("")
async def get_watchlist(request: Request) -> JSONResponse:
    try:
        return JSONResponse(jsonable(watchlist_of(_svc(request)).load()))
    except WatchlistError as exc:
        return _fail(422, str(exc))


@router.put("")
async def put_watchlist(request: Request) -> JSONResponse:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":           # a cross-site form post can't replace the list
        return _fail(415, "send JSON")
    try:
        body = await request.json()
    except ValueError:
        return _fail(400, "request body is not valid JSON")
    try:
        return JSONResponse(jsonable(watchlist_of(_svc(request)).save(body)))
    except WatchlistError as exc:
        return _fail(409, str(exc))
    except InputError as exc:
        return _fail(400, str(exc))


@router.get("/quotes")
async def quotes(request: Request, folder: str = "") -> JSONResponse:
    try:
        return JSONResponse(jsonable(await watchlist_of(_svc(request)).quotes(folder)))
    except WatchlistError as exc:
        return _fail(422, str(exc))
    except InputError as exc:
        return _fail(400, str(exc))
