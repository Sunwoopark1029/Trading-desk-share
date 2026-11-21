"""In-memory cache of the latest value per data source, with stale/error status."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

Status = Literal["ok", "stale", "error"]
MAX_ERROR_MESSAGE = 120


@dataclass(frozen=True)
class CacheEntry:
    value: Any                 # last good value (kept after errors)
    updated_at: float | None   # epoch seconds of the last good value
    source: str                # e.g. "toss", "yfinance", "fred"
    delay_label: str           # e.g. "real-time", "~15 min"
    poll_seconds: float
    status: Status
    error_code: str | None = None
    error_message: str | None = None   # short, never contains secrets or URLs


@dataclass
class _Slot:
    value: Any
    updated_at: float | None
    source: str
    delay_label: str
    poll_seconds: float
    error_code: str | None = None
    error_message: str | None = None


def _short(message: str | None) -> str | None:
    if not message:
        return None
    message = " ".join(str(message).split())
    return message if len(message) <= MAX_ERROR_MESSAGE else message[: MAX_ERROR_MESSAGE - 1] + "…"


class Cache:
    """Thread-safe key -> CacheEntry store. Stale = age > factor x poll_seconds."""

    def __init__(self, stale_after_factor: float = 3.0, clock: Callable[[], float] = time.time):
        self._factor = stale_after_factor
        self._clock = clock
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.Lock()

    def set_ok(self, key: str, value: Any, *, source: str, delay_label: str, poll_seconds: float) -> None:
        with self._lock:
            self._slots[key] = _Slot(value, self._clock(), source, delay_label, poll_seconds)

    def set_error(
        self,
        key: str,
        *,
        source: str,
        delay_label: str,
        poll_seconds: float,
        error_code: str,
        error_message: str | None = None,
    ) -> None:
        """Record an error; the last good value and its time are kept."""
        with self._lock:
            old = self._slots.get(key)
            self._slots[key] = _Slot(
                value=old.value if old else None,
                updated_at=old.updated_at if old else None,
                source=source,
                delay_label=delay_label,
                poll_seconds=poll_seconds,
                error_code=error_code,
                error_message=_short(error_message),
            )

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                return None
            return CacheEntry(
                value=slot.value,
                updated_at=slot.updated_at,
                source=slot.source,
                delay_label=slot.delay_label,
                poll_seconds=slot.poll_seconds,
                status=self._status(slot),
                error_code=slot.error_code,
                error_message=slot.error_message,
            )

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._slots)

    def _status(self, slot: _Slot) -> Status:
        if slot.error_code is not None:
            return "error"
        if slot.updated_at is None or self._clock() - slot.updated_at > self._factor * slot.poll_seconds:
            return "stale"
        return "ok"
