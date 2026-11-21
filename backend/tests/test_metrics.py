"""Metrics, peers and the fundamentals pack (phase 6 step 8): hand-calculated cases.

Test data is chosen so every input differs (cash, debt, equity, cash flow, capex, EBIT change by year; Toss shares,
statement shares outstanding and diluted shares are all different), so a value taken from the wrong period or the
wrong share count gives a wrong answer instead of passing by accident.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.fundamentals.metrics import all_metrics, cagr, implied, own_history_pe, valuation
from app.fundamentals.pack import PackValue, build_pack, display, fmt_money, fmt_pct
from app.fundamentals.peers import peer_medians, pick_peers
from app.fundamentals.statements import build

D = Decimal
YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
REV = [D("90"), D("100"), D("110"), D("121"), D("133.1"), D("146.41")]      # +10 % a year from 2021
TOSS_SHARES = D("12")


def f(y: int, val: Any, start: bool = True) -> dict[str, Any]:
    r = {"end": f"{y}-12-31", "val": val, "accn": f"k{y}", "form": "10-K", "fp": "FY", "fy": y,
         "filed": f"{y + 1}-02-15"}
    if start:
        r["start"] = f"{y}-01-01"
    return r


def company(ni: list[Any] | None = None, rev: list[Any] | None = None, shares_diluted: list[Any] | None = None,
            shares_out: list[Any] | None = None, unit: str = "USD", drop: tuple[str, ...] = (),
            equity: Any = None, ebit: Any = None, capex: Any = None, cash: Any = None) -> dict[str, Any]:
    rev = rev or REV
    ni = ni or [D("8"), D("10"), D("11"), D("12"), D("13"), D("20")]
    sd = shares_diluted or [10] * 6
    so = shares_out or [11] * 6
    g = {
        "Revenues": {unit: [f(y, v) for y, v in zip(YEARS, rev)]},
        "GrossProfit": {unit: [f(y, v * D("0.4")) for y, v in zip(YEARS, rev)]},
        "OperatingIncomeLoss": {unit: [f(y, ebit if ebit is not None else 25 + i) for i, y in enumerate(YEARS)]},
        "NetIncomeLoss": {unit: [f(y, v) for y, v in zip(YEARS, ni)]},
        "EarningsPerShareDiluted": {f"{unit}/shares": [f(y, D(v) / 10) for y, v in zip(YEARS, ni)]},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"shares": [f(y, v) for y, v in zip(YEARS, sd)]},
        "NetCashProvidedByUsedInOperatingActivities": {unit: [f(y, 35 + i) for i, y in enumerate(YEARS)]},
        "PaymentsToAcquirePropertyPlantAndEquipment": {unit: [f(y, capex if capex is not None else 5 + i)
                                                              for i, y in enumerate(YEARS)]},
        "CashAndCashEquivalentsAtCarryingValue": {unit: [f(y, cash if cash is not None else 45 + i, start=False)
                                                         for i, y in enumerate(YEARS)]},
        "LongTermDebtNoncurrent": {unit: [f(y, 75 + i, start=False) for i, y in enumerate(YEARS)]},
        "StockholdersEquity": {unit: [f(y, equity if equity is not None else 195 + i, start=False)
                                      for i, y in enumerate(YEARS)]},
        "CommonStockSharesOutstanding": {"shares": [f(y, v, start=False) for y, v in zip(YEARS, so)]},
    }
    for d in drop:
        g.pop(d)
    return {"cik": 1, "entityName": "Hand Co", "facts": {"us-gaap": {c: {"label": c, "units": u} for c, u in g.items()}}}


def m(metrics: list[Any], mid: str) -> Any:
    return next(x for x in metrics if x.id == mid)


# FY2025 (the TTM fallback: annual reports only): revenue 146.41, NI 20, EBIT 30, OCF 40, capex 10, cash 50,
# debt 80, equity 200, shares outstanding 11, Toss shares 12, price 30 -> mcap 360, EV 390

def test_growth_hand_calculated() -> None:
    st = build(company(), years=6)
    _, ms = all_metrics(st, D("30"), TOSS_SHARES)
    assert m(ms, "rev_yoy").value == D("0.1")                             # 146.41 / 133.1 - 1
    assert abs(m(ms, "rev_cagr_3y").value - D("0.1")) < D("1e-20")        # (146.41 / 110) ^ (1/3) - 1
    assert abs(m(ms, "rev_cagr_5y").value - D("0.1022133")) < D("0.0000001")   # (146.41 / 90) ^ (1/5) - 1
    assert m(ms, "ni_yoy").value == D("20") / D("13") - 1
    five = build(company(), years=5)                                      # 5 fiscal years: no 5y CAGR
    assert m(all_metrics(five, D("30"), TOSS_SHARES)[1], "rev_cagr_5y").note == "not enough history"


def test_cagr_negative_or_zero_start() -> None:
    assert cagr(D("-5"), D("10"), 3) == (None, "n/a (zero or negative start / end)")
    assert cagr(D("0"), D("10"), 3)[0] is None and cagr(D("10"), D("-1"), 3)[0] is None
    assert cagr(None, D("1"), 3) == (None, "not reported")
    val, _ = cagr(D("100"), D("121"), 2)
    assert abs(val - D("0.1")) < D("1e-25")


def test_margins_balance_and_valuation_hand_calculated() -> None:
    st = build(company(), years=6)
    _, ms = all_metrics(st, D("30"), TOSS_SHARES)
    assert m(ms, "gross_margin_ttm").value == D("0.4") and m(ms, "gross_margin_ttm").kind == "level"
    assert m(ms, "net_margin_ttm").value == D("20") / D("146.41")
    assert m(ms, "fcf_ttm").value == D("30") and m(ms, "fcf_margin_ttm").value == D("30") / D("146.41")
    assert m(ms, "net_debt").value == D("30") and m(ms, "roe").value == D("0.1")          # 80-50; 20/200
    assert m(ms, "mcap").value == D("360")                                # 30 x Toss shares 12 (not SEC 11)
    assert m(ms, "ev").value == D("390")                                  # 360 + 80 - 50
    assert m(ms, "pe_ttm").value == D("18")                               # 360 / 20
    assert m(ms, "ps_ttm").value == D("360") / D("146.41")
    assert m(ms, "pb").value == D("1.8") and m(ms, "ev_ebit_ttm").value == D("13")          # 390 / 30
    assert m(ms, "fcf_yield_ttm").value == D("30") / D("360")


def test_ttm_fallback_is_labelled_as_the_latest_fiscal_year() -> None:
    st = build(company(), years=6)                                        # annual reports only
    _, ms = all_metrics(st, D("30"), TOSS_SHARES)
    pe = m(ms, "pe_ttm")
    assert pe.label == "P/E (FY2025)" and pe.period == "FY2025" and "latest fiscal year" in pe.note
    assert m(ms, "gross_margin_ttm").label == "Gross margin (FY2025)"


def test_capex_reported_negative_still_subtracts() -> None:
    st = build(company(capex=-10), years=6)
    assert m(all_metrics(st, D("30"), TOSS_SHARES)[1], "fcf_ttm").value == D("30")          # 40 - |-10|


def test_loss_zero_revenue_negative_equity_and_operating_loss() -> None:
    loss = all_metrics(build(company(ni=[D("8")] * 5 + [D("-5")]), years=6), D("30"), TOSS_SHARES)[1]
    assert m(loss, "pe_ttm").value is None and m(loss, "pe_ttm").note == "n/a (loss)"
    zero = all_metrics(build(company(rev=[D("1")] * 5 + [D("0")]), years=6), D("30"), TOSS_SHARES)[1]
    assert m(zero, "net_margin_ttm").note.startswith("n/a (zero revenue)") and m(zero, "ps_ttm").note == "n/a (zero revenue)"
    assert m(zero, "rev_yoy").value == D("-1")                            # 0 / 1 - 1: a real -100 %
    neg = all_metrics(build(company(equity=-50), years=6), D("30"), TOSS_SHARES)[1]
    assert m(neg, "roe").note == "n/a (zero or negative equity)" and m(neg, "pb").note == "n/a (zero or negative equity)"
    opl = all_metrics(build(company(ebit=-30), years=6), D("30"), TOSS_SHARES)[1]
    assert m(opl, "ev_ebit_ttm").note == "n/a (operating loss)"
    burn = all_metrics(build(company(capex=60), years=6), D("30"), TOSS_SHARES)[1]
    assert m(burn, "fcf_yield_ttm").value < 0                             # negative FCF: a real negative yield


def test_missing_debt_or_cash_and_negative_ev() -> None:
    nodebt = all_metrics(build(company(drop=("LongTermDebtNoncurrent",)), years=6), D("30"), TOSS_SHARES)[1]
    assert m(nodebt, "ev").value is None and m(nodebt, "ev").note == "not reported"        # never debt = 0
    assert m(nodebt, "ev_ebit_ttm").note == "not reported" and m(nodebt, "net_debt").value is None
    rich = all_metrics(build(company(cash=1000), years=6), D("30"), TOSS_SHARES)[1]
    assert m(rich, "ev").value < 0 and m(rich, "ev_ebit_ttm").note == "n/a (zero or negative EV)"
    noprice = all_metrics(build(company(), years=6), None, TOSS_SHARES)[1]
    assert m(noprice, "ev").note == "no live price / shares" and m(noprice, "pe_ttm").note == "no live price / shares"


def test_split_refuses_eps_growth_only_inside_the_window() -> None:
    st = build(company(shares_diluted=[10, 10, 10, 10, 10, 100]), years=6)          # jump FY2024 -> FY2025
    assert any("stock split" in w for w in st.warnings)
    _, ms = all_metrics(st, D("30"), TOSS_SHARES)
    assert m(ms, "eps_yoy").value is None and "split" in m(ms, "eps_yoy").note
    assert m(ms, "rev_yoy").value == D("0.1") and m(ms, "pe_ttm").value == D("18")         # unaffected
    after = build(company(shares_diluted=[10, 10, 100, 100, 100, 100]), years=6)    # jump FY2021 -> FY2022
    _, ms = all_metrics(after, D("30"), TOSS_SHARES)
    assert m(ms, "eps_cagr_3y").value is not None                         # FY2022 -> FY2025: same basis
    assert m(ms, "shares_change_3y").value == D("0")
    assert m(ms, "eps_cagr_5y").value is None                             # FY2020 -> FY2025 crosses the jump


def test_non_usd_statements_get_no_multiples_and_dr_is_estimated() -> None:
    st = build(company(unit="TWD"), years=6)
    val = valuation(st, D("180"), TOSS_SHARES)
    assert all(x.value is None and "no verified exchange rate" in x.note for x in val.metrics)
    imp = implied(st, val, own_history_pe(st, {date(y, 12, 31): D("100") for y in YEARS}), D("15"))
    assert m(imp, "pe_own_median").value is None and "no verified exchange rate" in m(imp, "pe_own_median").note
    assert m(imp, "fv_pe_peer_median").value is None
    usd = build(company(), years=6)
    dr = valuation(usd, D("30"), TOSS_SHARES, security_type="DEPOSITARY_RECEIPT")
    assert m(dr.metrics, "mcap").note.startswith("est.") and m(dr.metrics, "pe_ttm").note.startswith("est.")
    imp = implied(usd, dr, own_history_pe(usd, {date(y, 12, 31): D("30") for y in YEARS}), D("15"))
    assert m(imp, "pe_own_median").note == "n/a (depositary receipt: ADS ratio unknown)"
    assert m(imp, "fv_pe_peer_median").note.startswith("est.")


def test_own_history_median_and_implied_price() -> None:
    st = build(company(shares_out=[11] * 6), years=6)
    val, _ = all_metrics(st, D("30"), D("11.2"))                          # Toss within 5 % of SEC's 11
    prices = {date(2023, 12, 31): D("12"), date(2024, 12, 31): D("13"), date(2025, 12, 31): D("40")}
    own, note = own_history_pe(st, prices)
    assert own == [("FY2025", D("22")), ("FY2024", D("11")), ("FY2023", D("11"))] and note is None   # price x 11 / NI
    imp = implied(st, val, (own, note), peer_pe_median=D("18"))
    assert m(imp, "pe_own_median").value == D("11")
    assert m(imp, "fv_pe_own_median").value == D("11") * 20 / D("11.2")   # median x NI / Toss shares
    assert m(imp, "fv_pe_peer_median").value == D("18") * 20 / D("11.2")
    short = implied(st, val, own[:2], None)
    assert m(short, "pe_own_median").value is None and "not enough history (2 of 3" in m(short, "pe_own_median").note
    assert m(short, "fv_pe_peer_median").note == "no peer P/E"
    differ = implied(st, valuation(st, D("30"), D("30")), (own, None), None)
    assert m(differ, "fv_pe_own_median").note == "n/a (Toss and SEC share counts differ)"


def test_own_history_stops_at_a_share_count_jump() -> None:
    st = build(company(shares_out=[1, 1, 1, 10, 10, 10]), years=6)        # 10x more shares from FY2023
    prices = {date(y, 12, 31): D("30") for y in YEARS}
    own, note = own_history_pe(st, prices)
    assert [k for k, _ in own] == ["FY2025", "FY2024", "FY2023"] and "older years not used" in note
    val, _ = all_metrics(st, D("30"), D("10"))
    imp = implied(st, val, (own, note), None)
    assert m(imp, "pe_own_median").value is not None and "older years not used" in m(imp, "pe_own_median").note


def test_peers_pick_and_medians() -> None:
    peers, note = pick_peers(["NVDA", "AAPL", "AVGO", "AMD", "QCOM", "INTC", "TXN"], "AAPL", 5)
    assert peers == ["NVDA", "AVGO", "AMD", "QCOM", "INTC"] and note is None
    few, note = pick_peers(["NVDA", "AAPL", "AMD"], "aapl", 5, allowed={"NVDA", "AMD", "AAPL"})
    assert few == ["NVDA", "AMD"] and note == "only 2 of 5 peers available in this sector"
    med = peer_medians({"A": {"pe_ttm": D("10")}, "B": {"pe_ttm": D("30")}, "C": {"pe_ttm": None}},
                       ["pe_ttm", "ps_ttm"])
    assert med[0].value == D("20") and (med[0].count, med[0].of) == (2, 3) and "2 of 3" in med[0].note
    assert med[1].value is None and med[1].note == "no peer has this metric"


def test_pack_ids_exact_display_source_and_as_of() -> None:
    st = build(company(), years=6)
    val, ms = all_metrics(st, D("30"), TOSS_SHARES)
    ms += implied(st, val, own_history_pe(st, {}), D("15"))
    pk = build_pack("hand", st, ms, statements_as_of="2026-02-15", price_as_of="2026-11-21T10:00:00+09:00",
                    peers_as_of="2026-11-20")
    ids = pk.ids()
    assert len(ids) == len(set(ids)) and {"ttm.revenue", "fy2025.revenue", "pe_ttm", "rev_cagr_3y"} <= set(ids)
    assert pk.get("pe_ttm").display == "18.0x" and pk.get("rev_yoy").display == "+10.0%"
    assert pk.get("fcf_yield_ttm").display == "8.3%" and pk.get("fy2025.revenue").display == "USD 146"
    assert pk.get("mcap").display == "USD 360"
    assert (pk.get("rev_yoy").source, pk.get("rev_yoy").as_of) == ("calculated (SEC EDGAR)", "2026-02-15")
    assert (pk.get("mcap").source, pk.get("mcap").as_of) == ("calculated (SEC EDGAR + Toss)", "2026-11-21T10:00:00+09:00")
    own = pk.get("pe_own_median")
    assert own.as_of == "2026-02-15" and "fiscal-year-end prices" in own.source        # not stamped as live
    peer = pk.get("pe_peer_median")
    assert peer.as_of == "2026-11-20" and peer.source.startswith("calculated (peer median")
    assert pk.get("fv_pe_own_median").display.startswith("not enough history")
    with pytest.raises(ValueError, match="duplicate"):
        build_pack("hand", st, ms, extra=[PackValue("pe_ttm", "dup", None, "x", "multiple", "TTM", "s", None)])


def test_display_formats() -> None:
    assert fmt_money(D("35123456789"), "USD") == "USD 35.1B" and fmt_money(D("-2.5e12"), "TWD") == "−TWD 2.5T"
    assert fmt_money(D("1234"), "USD") == "USD 1,234" and fmt_money(D("5.5e6"), "USD") == "USD 5.5M"
    assert fmt_money(D("999950000"), "USD") == "USD 1.0B" and fmt_money(D("999999.6"), "USD") == "USD 1.0M"
    assert fmt_pct(D("0.18234")) == "+18.2%" and fmt_pct(D("-0.05")) == "−5.0%"
    assert display(D("-0.032"), "level", None, None) == "−3.2%" and display(D("-4"), "multiple", None, None) == "−4.0x"
    assert display(D("15400000000"), "shares", None, None) == "15.40B shares"
    assert display(D("999995000"), "shares", None, None) == "1.00B shares"
    assert display(D("-1.5"), "per_share", "USD", None) == "−USD 1.50"
    assert display(D("245.125"), "per_share", "USD", None) == "USD 245.13"
    assert display(None, "pct", None, "n/a (loss)") == "n/a (loss)"
