"""Shared overlay format (ROADMAP.md) + validation + CRUD over overlays.json."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app import store
from app.timeutil import parse_iso_with_offset

OverlayType = Literal["hline", "trendline", "fib", "zone"]
OverlaySource = Literal["user", "indicator", "ai"]
POINTS_REQUIRED: dict[str, int] = {"hline": 1, "trendline": 2, "fib": 2, "zone": 2}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class OverlayPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: str      # ISO 8601 with UTC offset
    price: Decimal

    @field_validator("time")
    @classmethod
    def _time_has_offset(cls, v: str) -> str:
        parse_iso_with_offset(v)
        return v

    @field_validator("price")
    @classmethod
    def _price_positive(cls, v: Decimal) -> Decimal:
        if not v.is_finite() or v <= 0:
            raise ValueError("price must be a finite number greater than 0")
        return v


class Overlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id, pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    type: OverlayType
    points: list[OverlayPoint]
    label: str = Field("", max_length=200)
    color: str = Field(..., pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
    source: OverlaySource
    visible: bool = True
    note: str | None = Field(None, max_length=2000)
    # Ticker the line belongs to (upper-case). None = shown on every ticker.
    symbol: str | None = Field(None, pattern=r"^[A-Z0-9.\-]{1,20}$")
    # Chart timeframe the line belongs to (user choice 2026-11-20). None = shown on every timeframe.
    timeframe: Literal["1m", "5m", "1d"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("id"):
            data = {**data, "id": new_id()}
        return data

    @model_validator(mode="after")
    def _point_count(self) -> Overlay:
        need = POINTS_REQUIRED[self.type]
        if len(self.points) != need:
            raise ValueError(f"{self.type} needs exactly {need} point(s), got {len(self.points)}")
        return self


TF_IN_LABEL = (("1m", re.compile(r"(?<!\d)1\s*분|(?<!\d)1\s*m(?:in(?:ute)?s?)?\b", re.I)),
               ("5m", re.compile(r"(?<!\d)5\s*분|(?<!\d)5\s*m(?:in(?:ute)?s?)?\b", re.I)),
               ("1d", re.compile(r"일봉|일간|\bdaily\b|(?<!\d)1\s*d\b", re.I)))


def infer_timeframe(label: str | None) -> str | None:
    """Timeframe named in an old line's label ("AI 1분 피보나치" -> 1m), only when exactly one is named."""
    found = {tf for tf, rx in TF_IN_LABEL if rx.search(label or "")}
    return found.pop() if len(found) == 1 else None


def effective_timeframe(o: Overlay) -> str | None:
    """The line's timeframe; lines saved before the field existed: guessed from the label."""
    return o.timeframe or infer_timeframe(o.label)


class OverlayError(Exception):
    """Invalid overlay input (message is safe to show)."""


def parse_overlay(data: dict[str, Any] | Overlay) -> Overlay:
    if isinstance(data, Overlay):
        return data
    try:
        return Overlay.model_validate(data)
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'overlay'}: {e['msg']}" for e in exc.errors())
        raise OverlayError(msgs) from exc


def list_overlays(path: Path = store.OVERLAYS_PATH) -> list[Overlay]:
    out: list[Overlay] = []
    for i, raw in enumerate(store.read_overlays(path)):
        try:
            out.append(Overlay.model_validate(raw))
        except ValidationError as exc:
            raise store.StoreError(f"{path.name}: overlay #{i + 1} is invalid; fix or remove it") from exc
    return out


def get_overlay(overlay_id: str, path: Path = store.OVERLAYS_PATH) -> Overlay | None:
    return next((o for o in list_overlays(path) if o.id == overlay_id), None)


def save_overlay(data: dict[str, Any] | Overlay, path: Path = store.OVERLAYS_PATH) -> Overlay:
    """Add an overlay, or replace the one with the same id."""
    overlay = parse_overlay(data)
    items = [o for o in list_overlays(path) if o.id != overlay.id]
    items.append(overlay)
    store.write_overlays([o.model_dump() for o in items], path)
    return overlay


def delete_overlay(overlay_id: str, path: Path = store.OVERLAYS_PATH) -> bool:
    """Remove by id. Returns False if it did not exist."""
    items = list_overlays(path)
    kept = [o for o in items if o.id != overlay_id]
    if len(kept) == len(items):
        return False
    store.write_overlays([o.model_dump() for o in kept], path)
    return True
