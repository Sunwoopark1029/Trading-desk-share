import json
from decimal import Decimal

import pytest

from app import store
from app.store import StoreError, read_positions, save_plans, validate_plan


def test_reads_bom_and_keeps_decimal_exact(tmp_path):
    p = tmp_path / "positions.json"
    p.write_bytes(b"\xef\xbb\xbf" + b'{"trades": [{"price": 4.12, "fx_rate": 1458.30}]}')
    data = read_positions(p)
    assert data["trades"][0]["price"] == Decimal("4.12")
    assert str(data["trades"][0]["fx_rate"]) == "1458.30"
    assert data["plans"] == [] and data["avg_override"] == {}


def test_missing_file_gives_empty_sections(tmp_path):
    data = read_positions(tmp_path / "positions.json")
    assert data == {"trades": [], "plans": [], "avg_override": {}}


def test_invalid_json_raises_and_is_not_overwritten(tmp_path):
    p = tmp_path / "positions.json"
    broken = '{"trades": [ {"price": 4.12, } '
    p.write_text(broken, encoding="utf-8")
    with pytest.raises(StoreError, match="not valid JSON"):
        read_positions(p)
    with pytest.raises(StoreError):
        save_plans([{"symbol": "SOXS", "side": "buy", "price": "3.95", "currency": "USD"}], p)
    assert p.read_text(encoding="utf-8") == broken


def test_save_plans_replaces_only_plans(tmp_path):
    p = tmp_path / "positions.json"
    p.write_text(
        '{"trades": [{"price": 4.10}], "plans": [{"old": 1}], '
        '"avg_override": {"SOXS": {"price": 4.1, "currency": "USD"}}, "_readme": "x"}',
        encoding="utf-8-sig",
    )
    saved = save_plans([{"symbol": "soxs", "side": "sell", "price": 6500, "currency": "KRW"}], p)
    assert saved == [{"symbol": "SOXS", "side": "sell", "price": Decimal("6500"), "currency": "KRW"}]
    data = read_positions(p)
    assert data["plans"] == saved
    assert str(data["trades"][0]["price"]) == "4.10"  # untouched, exact
    assert data["avg_override"]["SOXS"]["price"] == Decimal("4.1")
    assert data["_readme"] == "x"
    assert not list(tmp_path.glob("*.tmp"))


def test_decimal_written_as_exact_number(tmp_path):
    p = tmp_path / "positions.json"
    save_plans([{"symbol": "SOXS", "side": "buy", "price": "3.950", "currency": "USD", "qty": "1.5"}], p)
    raw = p.read_text(encoding="utf-8")
    assert '"price": 3.950' in raw
    assert json.loads(raw)["plans"][0]["qty"] == 1.5


@pytest.mark.parametrize(
    "plan",
    [
        {"symbol": "SOXS", "side": "buy", "price": "3.95"},  # no currency
        {"symbol": "SOXS", "side": "buy", "price": "3.95", "currency": "EUR"},
        {"symbol": "SOXS", "side": "buy", "currency": "USD"},  # no price
        {"symbol": "SOXS", "side": "buy", "price": 0, "currency": "USD"},
        {"symbol": "SOXS", "side": "buy", "price": "-1", "currency": "USD"},
        {"symbol": "SOXS", "side": "buy", "price": "abc", "currency": "USD"},
        {"symbol": "SOXS", "side": "buy", "price": True, "currency": "USD"},
        {"symbol": "SOXS", "side": "buy", "price": "NaN", "currency": "USD"},
        {"symbol": "SOXS", "price": "3.95", "currency": "USD"},  # no side
        {"symbol": "SO XS", "side": "buy", "price": "3.95", "currency": "USD"},
    ],
)
def test_validate_plan_rejects_bad_input(plan):
    with pytest.raises(StoreError):
        validate_plan(plan)


def test_float_price_converted_exactly():
    plan = validate_plan({"symbol": "SOXS", "side": "buy", "price": 3.95, "currency": "USD"})
    assert plan["price"] == Decimal("3.95")


def test_overlays_file_roundtrip_and_broken_file_kept(tmp_path):
    p = tmp_path / "overlays.json"
    assert store.read_overlays(p) == []
    store.write_overlays([{"id": "a", "price": Decimal("1.10")}], p)
    assert store.read_overlays(p) == [{"id": "a", "price": Decimal("1.10")}]
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(StoreError):
        store.write_overlays([], p)
    assert p.read_text(encoding="utf-8") == "{oops"


def test_non_utf8_file_is_store_error_and_not_overwritten(tmp_path):
    p = tmp_path / "positions.json"
    raw = '{"trades": [], "plans": [], "note": "\ub9e4\uc218"}'.encode("cp949")
    p.write_bytes(raw)
    with pytest.raises(StoreError, match="not UTF-8"):
        read_positions(p)
    plan = {"symbol": "SOXS", "side": "buy", "price": "3.95", "currency": "USD"}
    with pytest.raises(StoreError):
        save_plans([plan], p)
    assert p.read_bytes() == raw


@pytest.mark.parametrize("const", ["NaN", "Infinity", "-Infinity"])
def test_nan_infinity_rejected_and_not_overwritten(tmp_path, const):
    p = tmp_path / "positions.json"
    text = '{"trades": [{"price": %s}], "plans": []}' % const
    p.write_text(text, encoding="utf-8")
    with pytest.raises(StoreError, match="NaN/Infinity"):
        read_positions(p)
    plan = {"symbol": "SOXS", "side": "buy", "price": "3.95", "currency": "USD"}
    with pytest.raises(StoreError):
        save_plans([plan], p)
    assert p.read_text(encoding="utf-8") == text
