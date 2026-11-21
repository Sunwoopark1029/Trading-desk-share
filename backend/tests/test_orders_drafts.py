"""Phase 4 part 2: draft validation rules, summaries, expiry, settings, routes. Nothing is sent."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orders.drafts import DraftStore, SettingsStore, draft_lines, summarize
from app.orders.validate import Context, parse_number, price_dp_ok, validate
from app.settings import OrdersConfig
from tests.test_api import LOCAL, make_services

D = Decimal


def ctx(**kw: Any) -> Context:
    base = dict(current_price=D("57.50"), price_source="test", session="regular", in_fraction_window=True,
                held_qty=D("10"), buying_power_usd=D("1000"), fx_mid=D("1400"), today_kst=date(2026, 11, 17),
                price_band_pct=D("3"), fee_rate=D("0.001"), fee_free_below_usd=D("10"),
                allow_market_outside_regular=True)
    base.update(kw)
    return Context(**base)


def codes(res: Any) -> list[str]:
    return [i.code for i in res.errors]


def wcodes(res: Any) -> list[str]:
    return [i.code for i in res.warnings]


BUY = {"kind": "BUY", "symbol": "SOXS", "currency": "USD", "order_type": "LIMIT", "price": "57.47",
       "size_mode": "shares", "quantity": "10", "time_in_force": "DAY"}


def test_valid_limit_buy_keeps_exact_numbers_and_estimates() -> None:
    r = validate(BUY, ctx())
    assert r.ok and r.normalized["price"] == D("57.47") and r.normalized["quantity"] == D("10")
    assert r.estimate["notional_usd"] == "574.70" and r.estimate["fee_usd_est"] == "0.57"
    assert r.estimate["total_usd_est"] == "575.27" and r.estimate["krw_ref"] == "804580"


@pytest.mark.parametrize("change,code", [
    ({"currency": "KRW"}, "currency_usd_required"),
    ({"currency": ""}, "currency_usd_required"),
    ({"order_type": ""}, "order_type_required"),
    ({"price": ""}, "price_required"),
    ({"price": "-1"}, "price_positive"),
    ({"price": "57.475"}, "price_decimals"),            # Toss would silently cut it: we refuse
    ({"price": "60.00"}, "price_out_of_band"),           # +4.3% > 3%
    ({"quantity": ""}, "quantity_required"),
    ({"quantity": "0"}, "quantity_positive"),
    ({"quantity": "1.5"}, "fraction_market_sell_only"),
    ({"time_in_force": ""}, "tif_required"),
    ({"time_in_force": "GTC"}, "tif_required"),
    ({"quantity": "100"}, "not_enough_buying_power"),    # 5,747 > 1,000 (LIMIT: error)
    ({"size_mode": "weird"}, "size_mode_invalid"),
    ({"kind": "HOLD"}, "kind_invalid"),
])
def test_each_rule(change: dict[str, Any], code: str) -> None:
    assert code in codes(validate({**BUY, **change}, ctx()))


def test_price_decimal_rules() -> None:
    assert price_dp_ok(D("57.47")) and not price_dp_ok(D("57.475"))
    assert price_dp_ok(D("0.1234")) and not price_dp_ok(D("0.12345"))
    assert parse_number("1,234.50") == D("1234.50") and parse_number("abc") is None and parse_number("nan") is None


def test_market_rules_and_sessions() -> None:
    mkt = {**BUY, "order_type": "MARKET", "price": ""}
    assert validate(mkt, ctx()).ok
    assert "market_no_price" in codes(validate({**mkt, "price": "57"}, ctx()))
    assert "cls_limit_only" in codes(validate({**mkt, "time_in_force": "CLS"}, ctx()))
    pre = validate(mkt, ctx(session="pre"))
    assert pre.ok and "market_thin_book" in wcodes(pre)                     # allowed by user choice
    assert "market_regular_only" in codes(validate(mkt, ctx(session="pre", allow_market_outside_regular=False)))
    assert "market_closed_now" in wcodes(validate(BUY, ctx(session=None)))
    big = validate({**mkt, "quantity": "100"}, ctx())
    assert big.ok and "not_enough_buying_power" in wcodes(big)              # MARKET: warning only


def test_no_current_price_blocks_limit_band_check() -> None:
    assert "no_current_price" in codes(validate(BUY, ctx(current_price=None)))


def test_sell_needs_shares_and_fraction_rules() -> None:
    sell = {**BUY, "kind": "SELL", "quantity": "11"}
    assert "not_enough_shares" in codes(validate(sell, ctx()))
    assert validate({**sell, "quantity": "10"}, ctx()).ok
    frac = {**sell, "order_type": "MARKET", "price": "", "quantity": "2.5"}
    assert validate(frac, ctx()).ok
    assert "fraction_outside_window" in codes(validate(frac, ctx(in_fraction_window=False)))
    assert "fraction_too_many_dp" in codes(validate({**frac, "quantity": "1.1234567"}, ctx()))


def test_percent_sizing_is_code_computed_whole_shares() -> None:
    r = validate({**BUY, "size_mode": "percent", "percent": "30", "quantity": ""}, ctx())
    assert r.ok and r.normalized["quantity"] == D("5")          # 1000*30% / 57.47 = 5.2 -> 5
    assert r.normalized["computed_from"]["buying_power_usd"] == "1000"
    s = validate({**BUY, "kind": "SELL", "size_mode": "percent", "percent": "50"}, ctx())
    assert s.normalized["quantity"] == D("5")                   # half of 10 held
    assert "percent_range" in codes(validate({**BUY, "size_mode": "percent", "percent": "120"}, ctx()))
    assert "percent_zero_shares" in codes(validate({**BUY, "size_mode": "percent", "percent": "1"}, ctx()))


def test_amount_orders_market_only_in_window() -> None:
    amt = {**BUY, "order_type": "MARKET", "price": "", "size_mode": "amount", "amount": "500"}
    assert validate(amt, ctx()).ok
    assert "amount_market_only" in codes(validate({**amt, "order_type": "LIMIT", "price": "57"}, ctx()))
    assert "amount_outside_window" in codes(validate(amt, ctx(in_fraction_window=False)))
    assert "amount_decimals" in codes(validate({**amt, "amount": "10.123"}, ctx()))


def test_high_value_needs_explicit_confirmation_and_optional_limits() -> None:
    rich = ctx(buying_power_usd=D("1000000"))
    big = {**BUY, "quantity": "1300"}                           # 74,711 USD * 1400 = 104.6M KRW
    assert "high_value_confirm" in codes(validate(big, rich))
    assert validate({**big, "confirm_high_value": True}, rich).ok
    assert "high_value_confirm" in codes(validate({**big, "confirm_high_value": "true"}, rich))  # must be real True
    assert "over_max_order" in codes(validate(BUY, ctx(max_order_usd=D("100"))))
    assert "over_max_day" in codes(validate(BUY, ctx(max_day_usd=D("600"), sent_today_usd=D("100"))))
    assert "over_max_orders" in codes(validate(BUY, ctx(max_orders_per_day=3, sent_today_count=3)))
    assert validate(BUY, ctx()).ok                               # limits off by default


COND = {"symbol": "SOXS", "currency": "USD", "order_type": "LIMIT", "size_mode": "shares", "quantity": "10",
        "expire_date": "2026-12-31"}


def test_conditional_single_oco_oto() -> None:
    single = {**COND, "kind": "COND_BUY", "legs": [{"trigger_price": "55.00", "order_price": "55.10"}]}
    r = validate(single, ctx())
    assert r.ok and r.normalized["legs"][0]["side"] == "BUY"
    assert validate({**single, "order_type": "MARKET", "legs": [{"trigger_price": "55"}]}, ctx()).ok
    assert "expire_date_past" in codes(validate({**single, "expire_date": "2026-11-16"}, ctx()))
    assert "expire_date_too_far" in codes(validate({**single, "expire_date": "2027-12-31"}, ctx()))
    assert "expire_date_required" in codes(validate({**single, "expire_date": ""}, ctx()))
    assert "fraction_conditional_off" in codes(validate({**single, "quantity": "1.5"}, ctx()))
    oco = {**COND, "kind": "OCO", "legs": [{"trigger_price": "60", "order_price": "59.9"},
                                           {"trigger_price": "55", "order_price": "54.9"}]}
    assert validate(oco, ctx()).ok
    assert "oco_order" in codes(validate({**oco, "legs": list(reversed(oco["legs"]))}, ctx()))
    assert "oco_oto_limit_only" in codes(validate({**oco, "order_type": "MARKET"}, ctx()))
    assert "not_enough_shares" in codes(validate({**oco, "quantity": "20"}, ctx()))
    oto = {**COND, "kind": "OTO", "legs": [{"trigger_price": "56", "order_price": "56.1"},
                                           {"trigger_price": "59", "order_price": "58.9"}]}
    r = validate(oto, ctx())
    assert r.ok and [l["side"] for l in r.normalized["legs"]] == ["BUY", "SELL"]


def test_summaries_use_only_validated_values() -> None:
    en, ko = summarize(validate(BUY, ctx()).normalized)
    assert en == "Buy 10 SOXS at 57.47 USD, limit, valid today"
    assert ko == "SOXS 10주 57.47 USD 지정가 매수 (당일)"
    p = validate({**BUY, "size_mode": "percent", "percent": "30"}, ctx()).normalized
    assert summarize(p)[0].startswith("Buy 30% of buying power = 5 SOXS")
    oco = validate({**COND, "kind": "OCO", "legs": [{"trigger_price": "60", "order_price": "59.9"},
                                                    {"trigger_price": "55", "order_price": "54.9"}]}, ctx())
    en, ko = summarize(oco.normalized)
    assert "Take-profit / stop for 10 SOXS" in en and "the other is cancelled" in en and "익절/손절" in ko


def test_draft_expiry_discard_and_dashed_lines() -> None:
    now = [1000.0]
    store = DraftStore(clock=lambda: now[0])
    d = store.create(BUY, ctx(), ttl=180, source="panel", dry_run=True)
    bad = store.create({**BUY, "price": "1"}, ctx(), ttl=180, source="panel", dry_run=True)
    assert d.status == "draft" and bad.status == "invalid"
    assert {x.id for x in store.active("SOXS")} == {bad.id, d.id}
    lines = draft_lines(store.active(), "2026-11-17T01:00:00+09:00")
    assert len(lines) == 1 and lines[0]["dashed"] and lines[0]["points"][0]["price"] == "57.47"
    assert d.view(now[0])["seconds_left"] == 180
    now[0] += 181
    assert store.active() == [] and d.view(now[0])["status"] == "expired"
    assert store.discard(d.id) and not store.discard("nope")


def test_settings_store_reads_live_config() -> None:
    cfg = OrdersConfig()
    st = SettingsStore(cfg)
    assert st.get().price_band_pct == D("3") and st.get().draft_ttl_seconds == 180
    cfg.price_band_pct, cfg.draft_ttl_seconds = D("1.5"), 300      # what the Settings page does
    assert st.get().price_band_pct == D("1.5") and st.get().draft_ttl_seconds == 300


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_draft_routes(tmp_path: Path) -> None:
    from app.orders.drafts import SettingsStore as SS

    svc = make_services(tmp_path)
    await svc.refresh_stocks()
    await svc.refresh_quotes()
    svc.drafts = DraftStore(clock=svc.clock)
    svc.order_settings = SS(svc.config.orders)
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        r = c.post("/api/orders/drafts", json={**BUY, "price": "4.25", "symbol": "soxs", "source": "panel"})
        assert r.status_code == 201
        d = r.json()
        assert d["status"] == "draft" and d["dry_run"] is True and d["values"]["price"] == "4.25"
        assert d["inputs"]["price"] == "4.25" and d["context"]["current_price"] == "4.25"
        assert d["summary_en"] == "Buy 10 SOXS at 4.25 USD, limit, valid today"
        listed = c.get("/api/orders/drafts", params={"symbol": "SOXS"}).json()
        assert listed["drafts"][0]["id"] == d["id"] and listed["overlays"][0]["dashed"] is True
        bad = c.post("/api/orders/drafts", json={**BUY, "price": "4.255"}).json()
        assert bad["status"] == "invalid" and bad["errors"][0]["code"] == "price_decimals"
        assert c.post("/api/orders/drafts", json={**BUY, "symbol": "NOPE!"}).status_code == 400
        assert c.post("/api/orders/drafts", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
        evil = {"Origin": "https://evil.example"}
        assert c.post("/api/orders/drafts", json=BUY, headers=evil).status_code == 403
        assert c.delete(f"/api/orders/drafts/{d['id']}").status_code == 200
        assert c.get("/api/orders/drafts").json()["drafts"][0]["id"] == bad["id"]
        assert c.get("/api/orders/settings").json()["price_band_pct"] == "3"
