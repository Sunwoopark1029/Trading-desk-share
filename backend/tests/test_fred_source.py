"""FRED DGS10 tests with httpx.MockTransport (no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.data.fred_source import FRED_OBSERVATIONS_URL, FredError, latest_dgs10

KEY = "fredkey0123456789abcdef"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def all_text(err: BaseException) -> str:
    parts = [str(err), repr(err), str(err.args)]
    for linked in (err.__cause__, err.__context__):
        if linked is not None:
            parts += [str(linked), repr(linked)]
    return " ".join(parts)


@pytest.mark.anyio
async def test_skips_dot_and_returns_latest() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"observations": [
            {"date": "2026-11-13", "value": "."},
            {"date": "2026-11-12", "value": "4.12"},
            {"date": "2026-11-11", "value": "4.10"},
        ]})

    async with client_for(handler) as client:
        day, value = await latest_dgs10(KEY, client)
    assert (day, value) == (date(2026, 11, 12), Decimal("4.12"))
    params = seen[0].url.params
    assert str(seen[0].url).startswith(FRED_OBSERVATIONS_URL)
    assert params["series_id"] == "DGS10" and params["sort_order"] == "desc"
    assert params["limit"] == "5" and params["file_type"] == "json"


@pytest.mark.anyio
async def test_all_missing_is_no_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"observations": [{"date": "2026-11-13", "value": "."}]})

    async with client_for(handler) as client:
        with pytest.raises(FredError) as ei:
            await latest_dgs10(KEY, client)
    assert ei.value.code == "no-data"


@pytest.mark.anyio
async def test_500_never_contains_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"error for {request.url}")

    async with client_for(handler) as client:
        with pytest.raises(FredError) as ei:
            await latest_dgs10(KEY, client)
    assert (ei.value.status, ei.value.code) == (500, "http-500")
    assert KEY not in all_text(ei.value)


@pytest.mark.anyio
async def test_transport_error_never_contains_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    async with client_for(handler) as client:
        with pytest.raises(FredError) as ei:
            await latest_dgs10(KEY, client)
    assert (ei.value.status, ei.value.code) == (None, "transport")
    assert ei.value.__context__ is None
    assert KEY not in all_text(ei.value)


@pytest.mark.anyio
async def test_bad_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with client_for(handler) as client:
        with pytest.raises(FredError) as ei:
            await latest_dgs10(KEY, client)
    assert ei.value.code == "bad-response"


@pytest.mark.anyio
async def test_empty_key_makes_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    async with client_for(handler) as client:
        with pytest.raises(FredError) as ei:
            await latest_dgs10("", client)
    assert ei.value.code == "no-key"
