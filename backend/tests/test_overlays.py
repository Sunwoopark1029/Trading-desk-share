from decimal import Decimal

import pytest

from app.overlays import OverlayError, delete_overlay, get_overlay, list_overlays, parse_overlay, save_overlay

T1 = "2026-11-16T22:30:00+09:00"
T2 = "2026-11-17T01:00:00+09:00"


def hline(**kw):
    return {"type": "hline", "points": [{"time": T1, "price": 4.12}], "label": "avg",
            "color": "#ff8800", "source": "user", **kw}


def test_hline_valid_and_id_generated():
    o = parse_overlay(hline())
    assert o.id and len(o.id) == 12
    assert o.points[0].price == Decimal("4.12")
    assert o.visible is True and o.note is None
    assert parse_overlay(hline(id="")).id != ""


@pytest.mark.parametrize("kind", ["trendline", "fib", "zone"])
def test_two_point_types(kind):
    pts = [{"time": T1, "price": "4.0"}, {"time": T2, "price": "4.5"}]
    assert len(parse_overlay({**hline(), "type": kind, "points": pts}).points) == 2
    with pytest.raises(OverlayError):
        parse_overlay({**hline(), "type": kind})  # only 1 point


@pytest.mark.parametrize(
    "bad",
    [
        {"points": [{"time": T1, "price": 1}, {"time": T2, "price": 2}]},  # hline with 2 points
        {"points": [{"time": "2026-11-16T22:30:00", "price": 1}]},  # naive time
        {"points": [{"time": T1, "price": 0}]},
        {"points": []},
        {"color": "red"},
        {"color": "#12345"},
        {"source": "bot"},
        {"type": "ray"},
        {"extra": 1},
        {"id": "bad id!"},
    ],
)
def test_invalid_overlays(bad):
    with pytest.raises(OverlayError):
        parse_overlay(hline(**bad))


def test_crud(tmp_path):
    path = tmp_path / "overlays.json"
    assert list_overlays(path) == []
    a = save_overlay(hline(id="a1"), path)
    b = save_overlay({**hline(), "type": "fib", "source": "ai", "note": "why",
                      "points": [{"time": T1, "price": "3.90"}, {"time": T2, "price": "4.40"}]}, path)
    assert [o.id for o in list_overlays(path)] == ["a1", b.id]
    assert get_overlay(b.id, path).points[1].price == Decimal("4.40")
    assert '"price": 4.40' in path.read_text(encoding="utf-8")

    save_overlay(hline(id="a1", label="moved", visible=False), path)  # replace same id
    got = get_overlay("a1", path)
    assert got.label == "moved" and got.visible is False
    assert len(list_overlays(path)) == 2

    assert delete_overlay(a.id, path) is True
    assert delete_overlay("nope", path) is False
    assert [o.id for o in list_overlays(path)] == [b.id]
