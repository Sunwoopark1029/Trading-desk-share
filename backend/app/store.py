"""positions.json / overlays.json at the repo root: exact Decimal read, atomic write.

Invalid JSON raises StoreError and the file is never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.settings import DATA_DIR

POSITIONS_PATH = DATA_DIR / "positions.json"
OVERLAYS_PATH = DATA_DIR / "overlays.json"

CURRENCIES = ("USD", "KRW")
PLAN_SIDES = ("buy", "sell")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]+$")

_write_lock = threading.Lock()


class StoreError(Exception):
    """File problem with a message that is safe to show the user."""


# ── reading ──────────────────────────────────────────────────────────────

def _nonfinite(path: Path) -> Any:
    """json.loads hook: NaN/Infinity are refused (they cannot be saved back)."""
    def reject(_name: str) -> Any:
        raise StoreError(f"{path.name} contains NaN/Infinity; fix it by hand; "
                         "the file was not changed.")
    return reject


def read_json(path: Path) -> Any | None:
    """Read a JSON file (BOM allowed) with floats as Decimal. Missing file -> None."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise StoreError(
            f"{path.name} is not UTF-8 text. Re-save it as UTF-8; the file was not changed."
        ) from exc
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_nonfinite(path))
    except json.JSONDecodeError as exc:
        raise StoreError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}). "
            "Fix it by hand; the file was not changed."
        ) from exc


def read_positions(path: Path = POSITIONS_PATH) -> dict[str, Any]:
    """Return {"trades": [...], "plans": [...], "avg_override": {...}} (+ any other keys)."""
    data = read_json(path)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise StoreError(f"{path.name} must contain a JSON object at the top level")
    data.setdefault("trades", [])
    data.setdefault("plans", [])
    data.setdefault("avg_override", {})
    return data


def read_overlays(path: Path = OVERLAYS_PATH) -> list[dict[str, Any]]:
    """Return the raw overlay list stored as {"overlays": [...]}."""
    data = read_json(path)
    if data is None:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("overlays", []), list):
        raise StoreError(f'{path.name} must look like {{"overlays": [...]}}')
    return list(data.get("overlays", []))


# ── writing ──────────────────────────────────────────────────────────────

def dumps(obj: Any, indent: int = 2, _level: int = 0) -> str:
    """JSON text with Decimal written as an exact number (no float rounding)."""
    pad, inner = " " * (indent * _level), " " * (indent * (_level + 1))
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise StoreError("cannot save a non-finite number")
        return str(obj)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [f"{inner}{json.dumps(str(k), ensure_ascii=False)}: {dumps(v, indent, _level + 1)}"
                 for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, (list, tuple)):
        if not obj:
            return "[]"
        items = [inner + dumps(v, indent, _level + 1) for v in obj]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write to a temp file in the same folder, then os.replace over the target."""
    text = dumps(data) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_overlays(items: list[dict[str, Any]], path: Path = OVERLAYS_PATH) -> None:
    with _write_lock:
        if path.exists():
            read_overlays(path)  # refuse to overwrite a broken file
        atomic_write_json(path, {"overlays": items})


# ── plans ────────────────────────────────────────────────────────────────

def to_decimal(value: Any, field: str) -> Decimal:
    """Exact Decimal from Decimal/int/str/float; bool and junk rejected."""
    if isinstance(value, bool) or value is None:
        raise StoreError(f"{field} is required and must be a number")
    if isinstance(value, float):
        value = str(value)
    try:
        d = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StoreError(f"{field} must be a number") from exc
    if not d.is_finite():
        raise StoreError(f"{field} must be a finite number")
    return d


def validate_plan(plan: Any) -> dict[str, Any]:
    """Check one plan. Currency, side and price are required; nothing is defaulted."""
    if not isinstance(plan, dict):
        raise StoreError("each plan must be an object")
    symbol = plan.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise StoreError("plan symbol is required (letters, digits, '.' or '-')")
    side = plan.get("side")
    if side not in PLAN_SIDES:
        raise StoreError("plan side must be 'buy' or 'sell'")
    currency = plan.get("currency")
    if currency not in CURRENCIES:
        raise StoreError("plan currency must be 'USD' or 'KRW'")
    price = to_decimal(plan.get("price"), "plan price")
    if price <= 0:
        raise StoreError("plan price must be greater than 0")
    out: dict[str, Any] = {"symbol": symbol.upper(), "side": side, "price": price, "currency": currency}
    if plan.get("qty") is not None:
        qty = to_decimal(plan["qty"], "plan qty")
        if qty <= 0:
            raise StoreError("plan qty must be greater than 0")
        out["qty"] = qty
    for key in ("id", "note"):
        if plan.get(key) is not None:
            if not isinstance(plan[key], str):
                raise StoreError(f"plan {key} must be text")
            out[key] = plan[key]
    return out


def save_plans(plans: list[Any], path: Path = POSITIONS_PATH) -> list[dict[str, Any]]:
    """Validate, re-read the file, replace only "plans", write atomically."""
    if not isinstance(plans, list):
        raise StoreError("plans must be a list")
    clean = [validate_plan(p) for p in plans]
    with _write_lock:
        data = read_json(path)  # raises on invalid JSON -> file untouched
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise StoreError(f"{path.name} must contain a JSON object at the top level")
        data["plans"] = clean
        atomic_write_json(path, data)
    return clean
