"""FRED DGS10 (official US 10-year yield, daily).

This product uses the FRED(R) API but is not endorsed or certified by the
Federal Reserve Bank of St. Louis.
The API key is a query parameter, so URLs must never reach logs or errors.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


class FredError(Exception):
    """FRED call failed. Carries only an HTTP status and a short code."""

    def __init__(self, status: int | None, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"fred error status={status} code={code}")

    def __repr__(self) -> str:
        return f"FredError(status={self.status!r}, code={self.code!r})"


async def latest_dgs10(api_key: str, client: httpx.AsyncClient) -> tuple[date, Decimal]:
    """Latest non-missing DGS10 observation as (date, percent value)."""
    if not api_key:
        raise FredError(None, "no-key")
    params = {
        "series_id": "DGS10",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "5",
    }
    failed = False
    try:
        resp = await client.get(FRED_OBSERVATIONS_URL, params=params)
    except httpx.HTTPError:
        failed = True
    if failed:  # raised outside the except block: no chained httpx error
        raise FredError(None, "transport")
    if resp.status_code != 200:
        raise FredError(resp.status_code, f"http-{resp.status_code}")
    try:
        observations = json.loads(resp.content)["observations"]
    except (ValueError, KeyError, TypeError):
        raise FredError(200, "bad-response") from None
    for obs in observations if isinstance(observations, list) else []:
        raw = obs.get("value") if isinstance(obs, dict) else None
        if not isinstance(raw, str) or raw.strip() in ("", "."):
            continue
        try:
            day, value = date.fromisoformat(obs["date"]), Decimal(raw.strip())
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if value.is_finite():
            return day, value
    raise FredError(200, "no-data")
