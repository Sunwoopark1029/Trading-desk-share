from decimal import Decimal as D

import pytest

from app.indicators.candles import Bar
from app.indicators.swings import swings

T = 1_763_283_600
M = 60


def bars_from(highs, lows=None):
    lows = lows if lows is not None else [0] * len(highs)  # flat lows never swing
    return [
        Bar(T + i * M, D(l), D(h), D(l), D(h), D(1), "regular")
        for i, (h, l) in enumerate(zip(highs, lows))
    ]


def const_lows(k):
    return [0] * k


def test_single_peak():
    out = swings(bars_from([1, 2, 3, 9, 3, 2, 1], const_lows(7)))
    assert out == [{"end": T + 3 * M, "price": D(9), "kind": "high"}]


def test_single_valley():
    lows = [5, 4, 3, 1, 3, 4, 5]
    out = swings(bars_from([100] * 7, lows))
    assert out == [{"end": T + 3 * M, "price": D(1), "kind": "low"}]


def test_flat_top_marks_first_bar_only():
    out = swings(bars_from([1, 2, 3, 9, 9, 2, 1, 0], const_lows(8)))
    assert out == [{"end": T + 3 * M, "price": D(9), "kind": "high"}]


def test_flat_bottom_marks_first_bar_only():
    out = swings(bars_from([100] * 8, [5, 4, 3, 1, 1, 4, 5, 6]))
    assert out == [{"end": T + 3 * M, "price": D(1), "kind": "low"}]


def test_needs_strictly_higher_than_before():
    # Equal high in the N bars before -> not a swing high.
    out = swings(bars_from([1, 9, 3, 9, 3, 2, 1], const_lows(7)))
    assert out == []


def test_edges_first_and_last_n_unmarked():
    # Highest bar in the first N and in the last N: never marked.
    assert swings(bars_from([9, 1, 2, 3, 2, 1, 0], const_lows(7))) == []
    assert swings(bars_from([1, 2, 3, 4, 5, 6, 9, 1, 1], const_lows(9))) == []
    # Same peak with N bars after it is marked.
    out = swings(bars_from([1, 2, 3, 4, 5, 6, 9, 1, 1, 1], const_lows(10)))
    assert out == [{"end": T + 6 * M, "price": D(9), "kind": "high"}]


def test_too_few_bars():
    assert swings(bars_from([1, 2, 3, 9, 3, 2], const_lows(6))) == []
    assert swings([]) == []


def test_outside_bar_is_both_high_and_low():
    highs = [5, 5, 5, 9, 5, 5, 5]
    lows = [3, 3, 3, 1, 3, 3, 3]
    out = swings(bars_from(highs, lows))
    assert [m["kind"] for m in out] == ["high", "low"]
    assert [m["price"] for m in out] == [D(9), D(1)]


def test_custom_n_and_invalid_n():
    out = swings(bars_from([1, 5, 1], const_lows(3)), n=1)
    assert out == [{"end": T + M, "price": D(5), "kind": "high"}]
    with pytest.raises(ValueError):
        swings([], n=0)
