"""P&L tests with hand-calculated numbers (fee 0.1 %, free below USD 10)."""

from datetime import datetime
from decimal import Decimal as D

import pytest

from app.pnl import (
    AvgOverride,
    FxQuote,
    PnlConfig,
    PnlError,
    compute_pnl,
    estimate_fee_usd,
    format_money,
)

CFG = PnlConfig(fee_rate=D("0.001"), fee_free_below_usd=D("10"))
NOW = datetime.fromisoformat("2026-11-16T22:00:00+09:00")
FX_NOW = FxQuote(D("1500"), datetime.fromisoformat("2026-11-16T21:59:00+09:00"))


def no_ref(_t):
    return None


def tr(i, side, price, qty, *, cur="USD", fx="1400", fee=None, fee_cur=None,
       time=None, sym="SOXS"):
    return {
        "id": str(i), "symbol": sym, "side": side,
        "time": time or f"2026-11-1{min(i, 6)}T10:{i:02d}:00+09:00",
        "price": price, "currency": cur, "qty": qty,
        "fee": fee, "fee_currency": fee_cur, "fx_rate": fx,
    }


def run(trades, live=None, cfg=CFG, ref=no_ref, override=None, period="all", fx=FX_NOW):
    return compute_pnl(trades, live or {}, fx, ref, cfg, NOW, override, period)


def test_partial_sell():
    res = run([tr(1, "buy", "20", "10"), tr(2, "sell", "25", "4", fx="1450")],
              live={"SOXS": D("22")})
    s = res.symbols[0]
    assert s.qty == D("6")
    assert s.avg_usd.value == D("20")
    assert s.realized_usd.value == D("20")            # 4 × (25 − 20)
    assert s.realized_usd.status == "confirmed"
    assert s.realized_krw.value == D("33000")         # 4 × (36250 − 28000)
    assert s.unrealized_usd.value == D("12")          # 6 × (22 − 20)
    assert s.unrealized_krw.value == D("30000")       # 6×22×1500 − 6×28000
    assert s.unrealized_krw.status == "est."
    assert s.fees_est_usd.value == D("0.3")           # 0.2 + 0.1
    assert s.fees_est_krw.value == D("425")           # 280 + 145
    assert s.net_usd.value == D("31.7")               # 20 + 12 − 0.3
    assert s.net_krw.value == D("62575")              # 33000 + 30000 − 425
    assert s.net_usd.status == "est."


def test_rebuy_changes_avg_sell_does_not():
    res = run([
        tr(1, "buy", "20", "10"), tr(2, "buy", "30", "10"),
        tr(3, "sell", "26", "5"), tr(4, "buy", "40", "5"),
    ])
    s = res.symbols[0]
    assert s.qty == D("20")
    assert s.avg_usd.value == D("28.75")              # (15×25 + 5×40) / 20
    assert s.realized_usd.value == D("5")             # 5 × (26 − 25)


def test_mixed_krw_usd():
    cfg = PnlConfig(D("0.001"), D("10"), fx_spread_bp=D("10"))
    res = run([
        tr(1, "buy", "28000", "1", cur="KRW", fx="1400"),  # = USD 20
        tr(2, "buy", "30", "1", fx="1500"),                # = KRW 45000
        tr(3, "sell", "35", "1", fx="1600"),               # = KRW 56000
    ], live={"SOXS": D("40")}, cfg=cfg)
    s = res.symbols[0]
    assert s.avg_usd.value == D("25")
    assert s.realized_usd.value == D("10")            # 35 − 25
    assert s.realized_krw.value == D("19500")         # 56000 − 73000/2
    assert s.avg_krw.value == D("36500")
    assert s.fees_est_usd.value == D("0.085")         # 0.02 + 0.03 + 0.035
    assert s.fees_est_krw.value == D("129")           # 28 + 45 + 56
    # FX cost only on the KRW-paid trade: 10 bp of 20 USD / 28000 KRW
    assert s.fx_cost_usd.value == D("0.02")
    assert s.fx_cost_krw.value == D("28")
    assert s.fx_cost_usd.status == "est."
    assert s.unrealized_usd.value == D("15")          # 40 − 25
    assert s.unrealized_krw.value == D("23500")       # 40×1500 − 36500
    assert s.net_usd.value == D("24.895")             # 10 + 15 − 0.085 − 0.02
    assert s.net_krw.value == D("42843")              # 19500 + 23500 − 129 − 28


def test_reference_fx_is_est_and_missing_marks_missing():
    trades = [tr(1, "buy", "20", "10", fx=None), tr(2, "sell", "25", "10", fx=None)]
    s = run(trades, ref=lambda t: D("1400")).symbols[0]
    assert s.realized_usd.status == "confirmed"
    assert s.realized_krw.value == D("70000")         # 10 × 5 × 1400
    assert s.realized_krw.status == "est."
    s = run(trades).symbols[0]
    assert s.realized_usd.value == D("50")
    assert s.realized_krw.value is None and s.realized_krw.status == "missing"


def test_fee_confirmed_vs_est():
    s = run([
        tr(1, "buy", "20", "10", fee="0.5", fee_cur="USD"),
        tr(2, "buy", "20", "10", fee="700", fee_cur="KRW"),
        tr(3, "sell", "21", "10"),
    ]).symbols[0]
    assert s.fees_confirmed_usd.value == D("1")       # 0.5 + 700/1400
    assert s.fees_confirmed_krw.value == D("1400")    # 700 + 700
    assert s.fees_confirmed_usd.status == "confirmed"
    assert s.fees_est_usd.value == D("0.21")          # 0.1 % of 210
    assert s.fees_est_usd.status == "est."
    assert s.fees_usd.value == D("1.21")
    assert s.fees_usd.status == "est."


def test_under_10_usd_is_free():
    assert estimate_fee_usd(D("9.99"), CFG) == 0
    assert estimate_fee_usd(D("10"), CFG) == D("0.01")
    s = run([tr(1, "buy", "3", "3"), tr(2, "buy", "10", "1")]).symbols[0]
    assert s.fees_est_usd.value == D("0.01")          # 9 → 0, 10 → 0.01


def test_full_exit_then_reentry_resets_avg():
    s = run([
        tr(1, "buy", "20", "10"), tr(2, "sell", "25", "10"), tr(3, "buy", "40", "5"),
    ], live={"SOXS": D("42")}).symbols[0]
    assert s.qty == D("5")
    assert s.avg_usd.value == D("40")
    assert s.avg_krw.value == D("56000")
    assert s.realized_usd.value == D("50")
    assert s.unrealized_usd.value == D("10")


def test_oversell_raises():
    with pytest.raises(PnlError, match="oversell"):
        run([tr(1, "buy", "20", "5"), tr(2, "sell", "21", "6")])
    with pytest.raises(PnlError):
        run([tr(1, "sell", "21", "1")])


def test_bad_records_raise():
    with pytest.raises(PnlError, match="offset"):
        run([tr(1, "buy", "20", "1", time="2026-11-16T10:00:00")])
    with pytest.raises(PnlError):
        run([tr(1, "hold", "20", "1")])
    with pytest.raises(PnlError):
        run([tr(1, "buy", "20", "1", cur="EUR")])


def test_avg_override_usd_and_krw():
    trades = [tr(1, "buy", "20", "10")]
    s = run(trades, live={"SOXS": D("22")}, override={"SOXS": D("18")}).symbols[0]
    assert s.avg_source == "typed"
    assert s.avg_usd.status == "typed"
    assert s.avg_usd_computed.value == D("20")
    assert s.unrealized_usd.value == D("40")          # 10 × (22 − 18)
    assert s.unrealized_usd.status == "typed"
    assert s.unrealized_krw.value == D("60000")       # 10×22×1500 − 10×18×1500
    s = run(trades, live={"SOXS": D("22")},
            override={"SOXS": AvgOverride(D("27000"), "KRW")}).symbols[0]
    assert s.avg_usd.value == D("18")                 # 27000 / 1500
    assert s.unrealized_krw.value == D("60000")       # 330000 − 270000


def test_net_total_across_symbols_and_live_missing():
    res = run([
        tr(1, "buy", "20", "10"), tr(2, "sell", "25", "10"),
        tr(3, "buy", "100", "1", sym="SOXX"),
    ], live={"SOXX": D("110")})
    t = res.total
    assert t.realized_usd.value == D("50")
    assert t.unrealized_usd.value == D("10")
    assert t.fees_usd.value == D("0.55")              # 0.2 + 0.25 + 0.1
    assert t.ai_credits_usd.value == 0
    assert t.net_usd.value == D("59.45")              # 50 + 10 − 0.55
    res = run([tr(1, "buy", "20", "10")])             # no live price
    assert res.total.unrealized_usd.status == "missing"
    assert res.total.net_usd.value is None


def test_period_today_filters_realized_and_fees():
    trades = [
        tr(1, "buy", "20", "10", time="2026-11-14T10:00:00+09:00"),
        tr(2, "sell", "25", "2", time="2026-11-15T09:30:00-05:00"),  # 11-15 23:30 KST
        tr(3, "sell", "30", "2", time="2026-11-15T20:00:00-05:00"),  # 11-16 10:00 KST
    ]
    s = run(trades, period="today").symbols[0]
    assert s.realized_usd.value == D("20")            # 2 × (30 − 20) only
    assert s.fees_usd.value == D("0.06")              # 0.1 % of 60 only
    assert s.qty == D("6") and s.avg_usd.value == D("20")
    assert run(trades).symbols[0].realized_usd.value == D("30")


def test_fx_spread_not_set_and_basis_point_fallback():
    trades = [tr(1, "buy", "28000", "1", cur="KRW")]
    s = run(trades).symbols[0]
    assert s.fx_cost_usd.value == 0 and s.fx_cost_usd.status == "not set"
    cfg = PnlConfig(D("0.001"), D("10"), basis_point_default=D("5"))
    res = run(trades, cfg=cfg)
    assert res.symbols[0].fx_cost_krw.value == D("14")  # 5 bp of 28000
    assert any("UNVERIFIED" in n for n in res.notes)


def test_negative_basis_point_is_still_a_cost():
    # spec: basisPoint = (rate - midRate) / midRate x 10000, can be negative (fixture -19.6)
    trades = [tr(1, "buy", "28000", "1", cur="KRW")]
    cfg = PnlConfig(D("0.001"), D("10"), basis_point_default=D("-19.6"))
    s = run(trades, cfg=cfg).symbols[0]
    assert s.fx_cost_krw.value == D("54.88")  # 19.6 bp of 28000, never negative
    assert s.fx_cost_usd.value > 0


def test_format_money():
    assert format_money(D("1234.565"), "USD") == "USD 1,234.57"
    assert format_money(D("1234567.5"), "KRW") == "KRW 1,234,568"
    assert format_money(D("-0.001"), "USD") == "USD 0.00"
    assert format_money(D("-12.5"), "USD") == "USD -12.50"
    assert format_money(None, "KRW") == "KRW —"
