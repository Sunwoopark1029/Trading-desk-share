"""Thesis objects (phase 6 part C, step C4): built from a code-made valuation card, viewed with code-made facts.

A thesis never holds a number the AI typed: its verdicts / summary / risks are the AI's (or the user's) words, and
every price in it is a pack value the valuation card was filled with. The view adds what code knows now: price moved
since the thesis was saved, whether a newer SEC filing exists, days to the next earnings date.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.research.store import research_store_of
from app.services import InputError, Services, jsonable
from app.timeutil import KST

US_EASTERN = ZoneInfo("America/New_York")

SUMMARY_MAX = 400

# words in the user's own message that ask for the thesis to be saved (user choice 2026-11-21: then the AI may save).
# Only requests count: "save it", "save this / the thesis / as thesis", "go ahead and save", "저장해줘", "기록해 둬",
# "남겨줘". Not: refusals ("don't save", "저장하지 마", "저장할 필요 없어"), questions ("should I save?"), talk about
# a saved thesis ("저장된 거", "저장해둔"), or the noun "store".
SAVE_RE = re.compile(
    r"(?<![a-z])save\s+(?:it|this|that|the\s+thesis|(?:it\s+)?as\s+(?:a\s+|the\s+|my\s+)?thesis)(?![a-z])"
    r"|(?:^|[.!,]\s*)(?:please\s+|ok(?:ay)?,?\s+|yes,?\s+)?save(?:\s+it)?\s*(?:[.!]|$)"
    r"|(?:go\s+ahead\s+and|please)\s+save(?![a-z])"
    r"|저장\s*해\s*(?:줘|주세요|줄래|줘요|둬|놔|주라)|저장\s*(?:부탁|해라|하자)|저장해\s*(?:[.!~]|$)"
    r"|기록\s*해\s*(?:줘|주세요|둬|놔)|남겨\s*(?:줘|주세요|둬|놔)",
    re.IGNORECASE | re.MULTILINE)
NO_SAVE_RE = re.compile(
    r"(?:don'?t|do\s+not|never|not|no\s+need\s+to|without|stop)\s+(?:to\s+)?sav(?:e|ing)"
    r"|저장\s*(?:은|도|을)?\s*(?:하지|안\s*해|말고|말아|하면\s*안|할\s*필요\s*없|ㄴㄴ|나중에|x\b)"
    r"|나중에\s*저장|기록\s*(?:은|도)?\s*하지|남기지\s*마",
    re.IGNORECASE)
QUESTION_RE = re.compile(r"(?<![a-z])(?:should|shall|can|could|would|do)\s+(?:i|we)\s+save(?![a-z])", re.IGNORECASE)


def asked_to_save(user_text: str) -> bool:
    text = (user_text or "").replace("\u2019", "'").replace("\u2018", "'")
    if NO_SAVE_RE.search(text) or QUESTION_RE.search(text):
        return False
    return bool(SAVE_RE.search(text))


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def thesis_from_card(card: dict[str, Any], *, summary: str | None = None,
                     show_on_chart: dict[str, bool] | None = None, conversation_id: str | None = None,
                     latest_filed: str | None = None, next_earnings: dict[str, Any] | None = None,
                     warnings: list[str] | None = None) -> dict[str, Any]:
    """The thesis JSON (without id / saved_at / saved_by, which the store adds)."""
    text = " ".join(str(summary if summary is not None else card.get("summary") or "").split())
    if not text:
        raise InputError("summary is empty")
    if len(text) > SUMMARY_MAX:
        raise InputError(f"summary is too long (max {SUMMARY_MAX} characters)")
    flags = show_on_chart or {}
    refs = []
    for r in card.get("reference_levels") or []:
        rid = str(r.get("id"))
        refs.append({"id": rid, "label": r.get("label"), "price": r.get("price"), "display": r.get("display"),
                     "currency": r.get("currency"), "source": r.get("source"), "as_of": r.get("as_of"),
                     "show_on_chart": bool(flags.get(rid, rid.startswith("fv_")))})
    px = card.get("price") or {}
    return {
        "symbol": str(card.get("symbol") or "").upper(),
        "verdict": dict(card.get("verdicts") or {}),
        "summary": text,
        "basis": [b.get("id") for b in card.get("basis") or []],
        "basis_display": [{"id": b.get("id"), "label": b.get("label"), "display": b.get("display")}
                          for b in card.get("basis") or []],
        "reference_levels": refs,
        "risks": list(card.get("risks") or []),
        "change_view": list(card.get("change_view") or []),
        "price_at_save": px.get("last"), "currency": px.get("currency"), "price_time": px.get("time"),
        "price_source": px.get("source") or "Toss", "price_status": px.get("status"),
        "statements_currency": card.get("statements_currency"),
        "data_as_of": card.get("data_as_of") or {},
        "latest_filed": latest_filed,
        "next_earnings": next_earnings,
        "conversation_id": conversation_id,
        "warnings": list(card.get("warnings") or []) + list(warnings or []),
    }


def edited(current: dict[str, Any], summary: str | None, show_on_chart: dict[str, bool] | None) -> dict[str, Any]:
    """A new version of the current thesis with the user's edits (summary text, chart flags); numbers unchanged."""
    data = {k: v for k, v in current.items() if k not in ("id", "saved_at", "saved_by", "current")}
    data["edited_by_user"] = True
    if summary is not None:
        text = " ".join(str(summary).split())
        if not text:
            raise InputError("summary is empty")
        if len(text) > SUMMARY_MAX:
            raise InputError(f"summary is too long (max {SUMMARY_MAX} characters)")
        data["summary"] = text
    if show_on_chart is not None:
        data["reference_levels"] = [{**r, "show_on_chart": bool(show_on_chart.get(r["id"], r.get("show_on_chart")))}
                                    for r in data.get("reference_levels", [])]
    return data


async def card_facts(svc: Services, symbol: str) -> tuple[str | None, dict[str, Any] | None]:
    """(latest SEC filing date, next earnings) for a new thesis, from the research service caches."""
    from app.research.service import research_of
    rs = research_of(svc)
    latest = None
    try:
        tb = (await rs.financials(symbol, "annual")).get("table")
        latest = tb.get("latest_filed") if tb else None
    except Exception:  # noqa: BLE001 - optional facts
        latest = None
    nxt = None
    try:
        nxt = (await rs.overview(symbol)).get("next_earnings")
    except Exception:  # noqa: BLE001
        nxt = None
    return (str(latest) if latest else None), jsonable(nxt) if nxt else None


async def save_card(svc: Services, card: dict[str, Any], *, saved_by: str, summary: str | None = None,
                    show_on_chart: dict[str, bool] | None = None, conversation_id: str | None = None,
                    warnings: list[str] | None = None) -> dict[str, Any]:
    sym = str(card.get("symbol") or "").upper()
    latest, nxt = await card_facts(svc, sym)
    data = thesis_from_card(card, summary=summary, show_on_chart=show_on_chart, conversation_id=conversation_id,
                            latest_filed=latest, next_earnings=nxt, warnings=warnings)
    store = research_store_of(svc)
    saved = store.save(sym, data, saved_by)
    who = "you" if saved_by == "user" else "the AI (on your request)"
    v = data["verdict"]
    store.add_note(sym, f"Thesis saved by {who}: own history {v.get('own_history')}, peers {v.get('peers')}, "
                        f"growth {v.get('growth')}.", "code")
    return saved


async def thesis_view(svc: Services, symbol: str, now: datetime | None = None) -> dict[str, Any]:
    """Current thesis + facts code knows now + history + notes."""
    from app.fundamentals.pack import fmt_pct
    from app.research.service import research_of

    store = research_store_of(svc)
    cur = store.current(symbol)
    facts: dict[str, Any] = {}
    if cur is not None:
        rs = research_of(svc)
        try:
            ov = await rs.overview(symbol)
        except Exception:  # noqa: BLE001
            ov = {}
        px = (ov or {}).get("price")
        last, base = _dec(px.get("last")) if px else None, _dec(cur.get("price_at_save"))
        same_cur = px is not None and px.get("currency") == cur.get("currency")
        pstatus = (((ov or {}).get("meta") or {}).get("price") or {}).get("status")
        if last is not None and base is not None and base > 0 and same_cur:
            moved = (last - base) / base
            facts["price_now"] = {"last": px.get("last"), "currency": px.get("currency"), "time": px.get("time"),
                                  "source": "Toss", "status": pstatus}
            facts["moved_since_save"] = {"value": moved, "display": fmt_pct(moved),
                                         "stale": pstatus not in (None, "ok") or cur.get("price_status") not in (None, "ok")}
        latest, nxt = await card_facts(svc, symbol)
        saved_filed = cur.get("latest_filed")
        facts["new_filing"] = (None if not latest or not saved_filed else latest > saved_filed)
        facts["latest_filed"] = latest
        nxt = nxt or cur.get("next_earnings")
        if nxt and nxt.get("date"):
            try:
                d = date.fromisoformat(str(nxt["date"]))
                today_us = (now or datetime.now(UTC)).astimezone(US_EASTERN).date()   # the date is a US date
                if d >= today_us:                                                     # a past date is not "next"
                    facts["next_earnings"] = {**nxt, "days_away": (d - today_us).days, "date_note": "US date"}
            except ValueError:
                pass
    return jsonable({"symbol": symbol, "thesis": cur, "facts": facts, "history": store.history(symbol),
                     "notes": store.notes(symbol), "memory_version": store.memory_version(symbol)})


def memory_for_chat(svc: Services, symbol: str) -> dict[str, Any] | None:
    """Short ticker memory for the chat context: current thesis (words + reference displays) and notes."""
    store = research_store_of(svc)
    cur = store.current(symbol)
    notes = store.notes(symbol)
    if cur is None and not notes:
        return None
    out: dict[str, Any] = {}
    if cur is not None:
        out["thesis"] = {"saved_at_kst": _kst(cur["saved_at"]), "saved_by": cur["saved_by"],
                         "verdict": cur.get("verdict"), "summary": cur.get("summary"),
                         "price_at_save": cur.get("price_at_save"), "currency": cur.get("currency"),
                         "price_time": cur.get("price_time"), "price_source": cur.get("price_source", "Toss"),
                         "reference_levels": [{"id": f"thesis_{r['id']}", "label": r.get("label"),
                                               "display": r.get("display")} for r in cur.get("reference_levels", [])],
                         "risks": cur.get("risks", [])}
    if notes:
        out["notes"] = [{"date_kst": _kst(n["created"])[:10], "source": n["source"], "text": n["text"]} for n in notes]
    return out


def desk_thesis(svc: Services, symbol: str) -> dict[str, Any] | None:
    """Short saved thesis for the Desk chat's <context>: verdicts, summary, saved time, reference level ids."""
    cur = research_store_of(svc).current(symbol)
    if cur is None:
        return None
    return {"saved_at_kst": _kst(cur["saved_at"]), "saved_by": cur["saved_by"], "verdict": cur.get("verdict"),
            "summary": cur.get("summary"), "price_at_save": cur.get("price_at_save"), "currency": cur.get("currency"),
            "reference_level_ids": [f"thesis_{r['id']}" for r in cur.get("reference_levels", [])]}


def _kst(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(KST).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return iso


def memory_numbers_text(memory: dict[str, Any] | None) -> str:
    """The parts of the memory the number check may treat as code data: reference displays and the saved price.
    The thesis summary / risks are words the AI (or the user) wrote, so they never vouch for a number."""
    if not memory or "thesis" not in memory:
        return ""
    th = memory["thesis"]
    return json.dumps({"price_at_save": th.get("price_at_save"),
                       "reference_levels": [r.get("display") for r in th.get("reference_levels", [])]})
