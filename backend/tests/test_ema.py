from decimal import Decimal as D

import pytest

from app.indicators.ema import ema


def ds(*xs):
    return [D(str(x)) for x in xs]


def test_seed_is_sma_and_earlier_values_none():
    out = ema(ds(10, 11, 12, 15, 13, 9), 3)
    assert out[:2] == [None, None]
    assert out[2] == D(11)  # (10+11+12)/3


def test_hand_calculated_period_3():
    # alpha = 2/4 = 0.5
    # i3: 11 + 0.5*(15-11) = 13; i4: 13 + 0.5*(13-13) = 13; i5: 13 + 0.5*(9-13) = 11
    assert ema(ds(10, 11, 12, 15, 13, 9), 3) == [None, None, D(11), D(13), D(13), D(11)]


def test_hand_calculated_period_4():
    # alpha = 2/5 = 0.4; seed (2+4+6+8)/4 = 5
    # i4: 5 + 0.4*(10-5) = 7; i5: 7 + 0.4*(5-7) = 6.2
    assert ema(ds(2, 4, 6, 8, 10, 5), 4) == [None, None, None, D(5), D(7), D("6.2")]


def test_hand_calculated_period_9():
    # alpha = 0.2; seed mean(1..9) = 5; next: 5 + 0.2*(10-5) = 6; then 6 + 0.2*(1-6) = 5
    out = ema(ds(*range(1, 11), 1), 9)
    assert out[:8] == [None] * 8
    assert out[8:] == [D(5), D(6), D(5)]


def test_too_few_bars_all_none():
    assert ema(ds(1, 2), 3) == [None, None]
    assert ema([], 9) == []


def test_exactly_period_bars():
    assert ema(ds(1, 2, 3), 3) == [None, None, D(2)]


def test_period_one_equals_closes():
    assert ema(ds("1.5", "2.5", "0.5"), 1) == ds("1.5", "2.5", "0.5")


def test_invalid_period():
    with pytest.raises(ValueError):
        ema(ds(1, 2, 3), 0)
