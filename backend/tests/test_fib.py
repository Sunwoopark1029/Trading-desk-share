from decimal import Decimal as D

from app.indicators.candles import Bar
from app.indicators.fib import FIB_RATIOS, FibPoint, default_fib, fib_levels

T = 1_763_283_600  # arbitrary epoch seconds


def prices(res):
    return [lv.price for lv in res.levels]


def bar(end, h, l, session="regular", o=None, c=None):
    o = l if o is None else o
    c = h if c is None else c
    return Bar(end, D(o), D(h), D(l), D(c), D(1), session)


def test_ratios():
    assert FIB_RATIOS == tuple(D(x) for x in ("0", "0.236", "0.382", "0.5", "0.618", "0.786", "1"))


def test_up_move_low_before_high():
    res = fib_levels(FibPoint(T, D(10)), FibPoint(T + 60, D(20)))
    assert res.direction == "up"
    assert (res.low, res.high) == (D(10), D(20))
    # high - r*10
    assert prices(res) == [D(x) for x in ("20", "17.640", "16.180", "15.0", "13.820", "12.140", "10")]


def test_down_move_high_before_low():
    res = fib_levels(FibPoint(T, D(20)), FibPoint(T + 60, D(10)))
    assert res.direction == "down"
    # low + r*10
    assert prices(res) == [D(x) for x in ("10", "12.360", "13.820", "15.0", "16.180", "17.860", "20")]


def test_user_points_any_order_sorted_by_time():
    # User clicked the later point first: still an up move (low at T, high at T+300).
    res = fib_levels(FibPoint(T + 300, D("4.50")), FibPoint(T, D("4.00")))
    assert res.direction == "up"
    assert (res.p1.time, res.p2.time) == (T, T + 300)
    assert prices(res)[0] == D("4.50")
    assert prices(res)[3] == D("4.25")  # 4.50 - 0.5*0.50
    assert prices(res)[-1] == D("4.00")


def test_default_fib_up_and_excludes_day_market():
    bars = [
        bar(T, 100, 1, session="day"),  # day market: ignored
        bar(T + 60, "11", "10", session="pre"),  # low 10 first
        bar(T + 120, "15", "12"),
        bar(T + 180, "20", "14", session="after"),  # high 20 later
        bar(T + 240, "50", "0.5", session=None),  # untagged: ignored
    ]
    res = default_fib(bars)
    assert res is not None
    assert res.direction == "up"
    assert (res.p1, res.p2) == (FibPoint(T + 60, D(10)), FibPoint(T + 180, D(20)))
    assert prices(res)[1] == D("17.640")


def test_default_fib_down():
    bars = [bar(T, "20", "18"), bar(T + 60, "17", "10")]
    res = default_fib(bars)
    assert res.direction == "down"
    assert (res.p1, res.p2) == (FibPoint(T, D(20)), FibPoint(T + 60, D(10)))


def test_default_fib_ties_take_earliest():
    bars = [bar(T, "20", "15"), bar(T + 60, "20", "10"), bar(T + 120, "19", "10")]
    res = default_fib(bars)
    # high 20 first at T, low 10 first at T+60 -> down
    assert (res.p1.time, res.p2.time, res.direction) == (T, T + 60, "down")


def test_default_fib_single_bar_uses_candle_color():
    assert default_fib([bar(T, "20", "10", o="11", c="19")]).direction == "up"
    assert default_fib([bar(T, "20", "10", o="19", c="11")]).direction == "down"


def test_default_fib_none_when_no_bars():
    assert default_fib([]) is None
    assert default_fib([bar(T, "20", "10", session="day")]) is None
