import calendar
from decimal import Decimal as D

from app.indicators.candles import Bar
from app.indicators.vwap import vwap, vwap_at

BASE = calendar.timegm((2026, 11, 16, 9, 0, 0))  # pre-market start (anchor 1)
M = 60
A1 = BASE
A2 = BASE + 10 * M


def bar(end, h, l, c, v, session):
    return Bar(end, D(c), D(h), D(l), D(c), D(v), session)


def test_anchor_day_excluded_and_reset():
    bars = [
        bar(BASE - M, 99, 99, 99, 1000, "day"),  # before anchor 1
        bar(BASE, 99, 99, 99, 1000, "day"),  # end == anchor -> still before it
        bar(BASE + 1 * M, 12, 9, 9, 100, "pre"),  # tp 10
        bar(BASE + 2 * M, 21, 18, 21, 300, "pre"),  # tp 20
        bar(BASE + 3 * M, 30, 30, 30, 0, "regular"),  # zero volume bar
        bar(BASE + 4 * M, 26, 22, 24, 100, "after"),  # tp 24
        bar(BASE + 5 * M, 1000, 1000, 1000, 10**6, "day"),  # day market: excluded
        bar(BASE + 6 * M, 1000, 1000, 1000, 10**6, None),  # untagged: excluded
        bar(A2 + 1 * M, 50, 50, 50, 10, "regular"),  # new period, resets
    ]
    res = vwap(bars, [A2, A1])  # anchor order does not matter
    assert res.values == [
        None,
        None,
        D(10),  # 10*100/100
        D("17.5"),  # (1000 + 6000) / 400
        D("17.5"),  # zero volume: unchanged
        D("18.8"),  # (7000 + 2400) / 500
        None,
        None,
        D(50),  # reset at anchor 2
    ]
    # Period 2 has no pre-market bars -> flagged, and it is the latest period.
    assert res.no_premarket_anchors == [A2]
    assert res.regular_only is True


def test_day_market_bar_does_not_change_later_values():
    with_day = [
        bar(BASE + 1 * M, 10, 10, 10, 100, "pre"),
        bar(BASE + 2 * M, 500, 500, 500, 9999, "day"),
        bar(BASE + 3 * M, 20, 20, 20, 100, "regular"),
    ]
    res = vwap(with_day, [A1])
    assert res.values == [D(10), None, D(15)]
    assert res.regular_only is False
    assert res.no_premarket_anchors == []


def test_zero_total_volume_is_none():
    bars = [
        bar(BASE + 1 * M, 10, 10, 10, 0, "pre"),
        bar(BASE + 2 * M, 10, 10, 10, 0, "pre"),
        bar(BASE + 3 * M, 30, 30, 30, 2, "pre"),
    ]
    assert vwap(bars, [A1]).values == [None, None, D(30)]


def test_non_terminating_typical_price():
    # tp = (1+1+2)/3 = 4/3
    res = vwap([bar(BASE + M, 1, 1, 2, 3, "pre")], [A1])
    assert res.values == [D(4) / D(3)]


def test_no_anchor_means_none():
    res = vwap([bar(BASE + M, 10, 10, 10, 5, "pre")], [])
    assert res.values == [None]
    assert res.regular_only is False


def test_regular_only_flag_without_premarket():
    res = vwap([bar(BASE + 3 * M, 10, 10, 10, 5, "regular")], [A1])
    assert res.values == [D(10)]
    assert res.regular_only is True
    assert res.no_premarket_anchors == [A1]


def test_vwap_at_samples_latest_bar_in_window():
    bars = [
        bar(BASE + 1 * M, 1, 1, 1, 1, "pre"),
        bar(BASE + 3 * M, 1, 1, 1, 1, "pre"),
        bar(BASE + 12 * M, 1, 1, 1, 1, "pre"),
    ]
    values = [D(1), D(2), D(3)]
    ends = [BASE + 5 * M, BASE + 10 * M, BASE + 15 * M, BASE]
    # 5m end BASE+5m -> bar at +3m; (5m,10m] empty -> None; +15m -> bar at +12m; BASE -> None
    assert vwap_at(bars, values, ends) == [D(2), None, D(3), None]
