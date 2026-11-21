from decimal import Decimal as D

import pytest

from app.indicators.rsi import rsi


def ds(*xs):
    return [D(str(x)) for x in xs]


def test_too_few_closes_all_none():
    assert rsi(ds(1, 2, 3), 3) == [None, None, None]
    assert rsi([], 14) == []


def test_hand_calculated_period_2():
    # changes: +2, -1, +3, -2
    # i2: avg_gain (2+0)/2 = 1, avg_loss (0+1)/2 = 0.5 -> RS 2 -> 100 - 100/3
    # i3: gain (1*1 + 3)/2 = 2, loss (0.5*1 + 0)/2 = 0.25 -> RS 8 -> 100 - 100/9
    # i4: gain (2 + 0)/2 = 1, loss (0.25 + 2)/2 = 1.125 -> RS 8/9 -> 100 - 100/(17/9)
    out = rsi(ds(10, 12, 11, 14, 12), 2)
    assert out[:2] == [None, None]
    assert out[2] == D(100) - D(100) / D(3)
    assert out[3] == D(100) - D(100) / D(9)
    assert abs(out[4] - (D(100) - D(900) / D(17))) < D("1e-20")


def test_only_rising_is_100_only_falling_is_0():
    assert rsi(ds(1, 2, 3, 4, 5), 3)[3:] == [D(100), D(100)]
    assert rsi(ds(5, 4, 3, 2, 1), 3)[3:] == [D(0), D(0)]


def test_flat_is_50():
    assert rsi(ds(7, 7, 7, 7), 2)[2:] == [D(50), D(50)]


def test_hand_calculated_period_3_with_smoothing():
    # changes: +2, -1, +2, 0, -3
    # i3: gain 4/3, loss 1/3 -> RS 4 -> 80
    # i4: gain (4/3*2 + 0)/3 = 8/9, loss (1/3*2 + 0)/3 = 2/9 -> RS 4 -> 80
    # i5: gain (8/9*2 + 0)/3 = 16/27, loss (2/9*2 + 3)/3 = 31/27 -> RS 16/31 -> 100 - 3100/47
    out = rsi(ds(1, 3, 2, 4, 4, 1), 3)
    assert out[:3] == [None, None, None]
    assert abs(out[3] - D(80)) < D("1e-20")
    assert abs(out[4] - D(80)) < D("1e-20")
    assert abs(out[5] - (D(100) - D(3100) / D(47))) < D("1e-20")


def test_values_stay_in_range():
    out = rsi(ds(3, 9, 1, 8, 2, 7, 7, 6, 10, 1, 5), 4)
    assert all(D(0) <= v <= D(100) for v in out if v is not None)


def test_bad_period():
    with pytest.raises(ValueError):
        rsi(ds(1, 2), 0)
