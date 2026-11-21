"""Explore routes (phase 6 part A). Read-only; Toss is called only when a view is requested.

GET /api/explore/rankings?type=&duration=&count=   stock ranking (US), names joined from stock basics
GET /api/explore/sectors                           the whole sector tree (cached a day)
GET /api/explore/sectors/rankings?type=&duration=  sector ranking (US)
GET /api/explore/sectors/{sector_id}               detail + stocks (by market cap) + ETFs, last prices for the top N
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.explore.views import explore_of
from app.services import InputError, Services, jsonable

router = APIRouter(prefix="/api/explore")


def _svc(request: Request) -> Services:
    return request.app.state.services


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _ok(data: object) -> JSONResponse:
    return JSONResponse(jsonable(data))


@router.get("/rankings")
async def rankings(request: Request, type: str = "MARKET_TRADING_VOLUME", duration: str = "1d",
                   count: int = 50) -> JSONResponse:
    try:
        return _ok(await explore_of(_svc(request)).rankings(type, duration, count))
    except InputError as exc:
        return _fail(400, str(exc))


@router.get("/sectors")
async def sectors(request: Request) -> JSONResponse:
    return _ok(await explore_of(_svc(request)).sectors())


@router.get("/sectors/rankings")      # declared before /sectors/{sector_id}: "rankings" is not an id
async def sector_rankings(request: Request, type: str = "TOP_GAINERS", duration: str = "1d") -> JSONResponse:
    try:
        return _ok(await explore_of(_svc(request)).sector_rankings(type, duration))
    except InputError as exc:
        return _fail(400, str(exc))


@router.get("/sectors/{sector_id}")
async def sector(request: Request, sector_id: str) -> JSONResponse:
    try:
        return _ok(await explore_of(_svc(request)).sector(sector_id))
    except InputError as exc:
        return _fail(400, str(exc))
