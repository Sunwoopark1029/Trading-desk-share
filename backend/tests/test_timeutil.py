from datetime import date, datetime, time, timezone

import pandas as pd
import pytest

from app.timeutil import (
    KST,
    NY,
    epoch_to_kst_iso,
    ny_datetime,
    parse_iso_with_offset,
    to_epoch,
    to_kst,
    us_trading_date,
)


def test_parse_keeps_offset_and_rejects_naive():
    dt = parse_iso_with_offset("2026-03-09T09:30:00-04:00")
    assert to_epoch(dt) == to_epoch("2026-03-09T13:30:00Z")
    with pytest.raises(ValueError):
        parse_iso_with_offset("2026-03-09T09:30:00")
    with pytest.raises(ValueError):
        parse_iso_with_offset("not a time")
    with pytest.raises(ValueError):
        to_epoch(datetime(2026, 3, 9, 9, 30))


def test_kst_offset_is_parsed_not_assumed():
    assert to_epoch("2026-11-16T22:30:00+09:00") == to_epoch("2026-11-16T13:30:00+00:00")


def test_epoch_to_kst_iso():
    epoch = int(datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc).timestamp())
    assert epoch_to_kst_iso(epoch) == "2026-11-16T23:30:00+09:00"


@pytest.mark.parametrize(
    "day, kst_open",
    [
        (date(2026, 3, 6), "2026-03-06T23:30:00+09:00"),   # Fri, EST
        (date(2026, 3, 9), "2026-03-09T22:30:00+09:00"),   # Mon, EDT
        (date(2026, 10, 30), "2026-10-30T22:30:00+09:00"), # Fri, EDT
        (date(2026, 11, 2), "2026-11-02T23:30:00+09:00"),  # Mon, EST
    ],
)
def test_regular_open_in_kst_across_dst(day, kst_open):
    open_ny = ny_datetime(day, time(9, 30))
    assert epoch_to_kst_iso(to_epoch(open_ny)) == kst_open


@pytest.mark.parametrize(
    "rollover_kst, prev_date, next_date",
    [
        # 20:00 ET = 10:00 KST (EST) / 09:00 KST (EDT)
        ("2026-03-06T10:00:00+09:00", date(2026, 3, 5), date(2026, 3, 6)),
        ("2026-03-09T09:00:00+09:00", date(2026, 3, 8), date(2026, 3, 9)),
        ("2026-10-30T09:00:00+09:00", date(2026, 10, 29), date(2026, 10, 30)),
        ("2026-11-02T10:00:00+09:00", date(2026, 11, 1), date(2026, 11, 2)),
    ],
)
def test_trading_date_boundary_across_dst(rollover_kst, prev_date, next_date):
    t = to_epoch(rollover_kst)
    assert us_trading_date(t - 1) == prev_date
    assert us_trading_date(t) == next_date


def test_day_market_belongs_to_next_trading_date():
    # Winter (EST): the Toss day market starts 10:00 KST (= 20:00 EST); it shifts with DST.
    # 09:30 KST Mon 2026-11-02 = Sun 19:30 EST -> still Sun's date;
    # 10:30 KST = 20:30 EST -> Monday's trading date.
    assert us_trading_date(to_epoch("2026-11-02T09:30:00+09:00")) == date(2026, 11, 1)
    assert us_trading_date(to_epoch("2026-11-02T10:30:00+09:00")) == date(2026, 11, 2)
    # pre-market 04:00 EST Mon = 18:00 KST Mon
    assert us_trading_date(to_epoch("2026-11-02T18:00:00+09:00")) == date(2026, 11, 2)


def test_yfinance_style_timestamp_to_kst():
    ts = pd.Timestamp("2026-03-09 09:30", tz="America/New_York")
    assert to_kst(ts).isoformat() == "2026-03-09T22:30:00+09:00"
    ts2 = pd.Timestamp("2026-03-06 09:30", tz="America/New_York")
    assert to_kst(ts2).isoformat() == "2026-03-06T23:30:00+09:00"


def test_zones():
    assert str(KST) == "Asia/Seoul" and str(NY) == "America/New_York"
