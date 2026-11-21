import calendar
from decimal import Decimal as D

from app.indicators.candles import (
    Bar,
    Session,
    aggregate_5m,
    bar_start,
    bucket_end,
    tag_sessions,
)

# 2026-11-16 09:00:00 UTC (a :00 boundary)
BASE = calendar.timegm((2026, 11, 16, 9, 0, 0))
M = 60


def bar(end, o, h, l, c, v, session=None):
    return Bar(end, D(o), D(h), D(l), D(c), D(v), session)


# ---- sessions ----

SESSIONS = [
    Session("pre", BASE, BASE + 30 * M),
    Session("regular", BASE + 30 * M, BASE + 60 * M),
]


def test_tag_sessions_boundaries():
    bars = [
        bar(BASE, 1, 1, 1, 1, 1),  # end == pre start -> not in pre (start exclusive)
        bar(BASE + M, 1, 1, 1, 1, 1),  # first pre bar
        bar(BASE + 30 * M, 1, 1, 1, 1, 1),  # end == pre end -> pre (end inclusive)
        bar(BASE + 31 * M, 1, 1, 1, 1, 1),  # first regular bar
        bar(BASE + 60 * M, 1, 1, 1, 1, 1),  # last regular bar
        bar(BASE + 61 * M, 1, 1, 1, 1, 1),  # outside every session
    ]
    tagged = tag_sessions(bars, SESSIONS)
    assert [b.session for b in tagged] == [None, "pre", "pre", "regular", "regular", None]
    # Original bars are untouched (frozen dataclass copies).
    assert bars[1].session is None


def test_tag_sessions_holiday_no_sessions_gives_none():
    tagged = tag_sessions([bar(BASE + M, 1, 1, 1, 1, 1)], [])
    assert tagged[0].session is None


# ---- 5m aggregation ----


def test_aggregate_normal_window():
    bars = [
        bar(BASE + 1 * M, "10.00", "10.50", "9.90", "10.20", 100),
        bar(BASE + 2 * M, "10.20", "10.80", "10.10", "10.70", 200),
        bar(BASE + 3 * M, "10.70", "10.75", "9.50", "9.60", 300),
        bar(BASE + 4 * M, "9.60", "10.00", "9.55", "9.90", 400),
        bar(BASE + 5 * M, "9.90", "10.10", "9.80", "10.05", 500),
    ]
    out = aggregate_5m(bars)
    assert len(out) == 1
    b = out[0]
    assert b.end == BASE + 5 * M
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        D("10.00"),
        D("10.80"),
        D("9.50"),
        D("10.05"),
        D(1500),
    )


def test_aggregate_missing_minutes():
    bars = [
        bar(BASE + 1 * M, "5", "6", "4", "5.5", 10),
        bar(BASE + 3 * M, "5.5", "7", "5", "6.5", 20),
    ]
    out = aggregate_5m(bars)
    assert len(out) == 1
    b = out[0]
    assert b.end == BASE + 5 * M
    assert (b.open, b.high, b.low, b.close, b.volume) == (D(5), D(7), D(4), D("6.5"), D(30))


def test_aggregate_empty_window_produces_no_bar():
    bars = [
        bar(BASE + 2 * M, 1, 2, 1, 2, 5),  # window ending BASE+5m
        # nothing in (BASE+5m, BASE+10m]
        bar(BASE + 12 * M, 3, 4, 3, 4, 7),  # window ending BASE+15m
    ]
    out = aggregate_5m(bars)
    assert [b.end for b in out] == [BASE + 5 * M, BASE + 15 * M]
    assert all(b.volume > 0 for b in out)  # no zero-filled bar


def test_aggregate_end_stamp_boundaries():
    # end exactly on T belongs to (T-5m, T]; end T+1m starts the next window.
    bars = [
        bar(BASE + 5 * M, 1, 1, 1, 1, 1),
        bar(BASE + 6 * M, 2, 2, 2, 2, 2),
        bar(BASE + 10 * M, 3, 3, 3, 3, 3),
    ]
    out = aggregate_5m(bars)
    assert [b.end for b in out] == [BASE + 5 * M, BASE + 10 * M]
    assert out[0].close == D(1)
    assert (out[1].open, out[1].close, out[1].volume) == (D(2), D(3), D(5))
    assert bucket_end(BASE + 1) == BASE + 5 * M
    assert bucket_end(BASE) == BASE


def test_aggregate_pre_to_regular_boundary():
    # Regular starts at BASE+30m; 1m bars ending 26..35 min.
    bars = tag_sessions(
        [bar(BASE + k * M, k, k, k, k, 1) for k in range(26, 36)], SESSIONS
    )
    out = aggregate_5m(bars)
    assert [(b.end, b.session) for b in out] == [
        (BASE + 30 * M, "pre"),
        (BASE + 35 * M, "regular"),
    ]
    assert (out[0].open, out[0].close) == (D(26), D(30))
    assert (out[1].open, out[1].close) == (D(31), D(35))


def test_aggregate_unsorted_and_duplicate_input():
    bars = [
        bar(BASE + 3 * M, 3, 3, 3, 3, 3),
        bar(BASE + 1 * M, 1, 1, 1, 1, 1),
        bar(BASE + 3 * M, 9, 9, 9, 9, 9),  # replaces the earlier +3m bar
    ]
    out = aggregate_5m(bars)
    assert len(out) == 1
    assert (out[0].open, out[0].close, out[0].high, out[0].volume) == (D(1), D(9), D(9), D(10))


def test_aggregate_empty_input():
    assert aggregate_5m([]) == []


def test_bar_start():
    b = bar(BASE + 5 * M, 1, 1, 1, 1, 1)
    assert bar_start(b, 300) == BASE
    assert bar_start(b, 60) == BASE + 4 * M
