"""SEC facts -> statement tables (phase 6 step 6). Hand-made cases per rule + a trimmed real Apple file."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.fundamentals.statements import build, read_facts, reporting_currency

D = Decimal
FIX = Path(__file__).parent / "fixtures"


def fact(start: str | None, end: str, val: Any, *, form: str = "10-K", fp: str = "FY", fy: int = 2025,
         filed: str = "2025-11-01", accn: str = "a1") -> dict[str, Any]:
    f = {"end": end, "val": val, "accn": accn, "form": form, "fp": fp, "fy": fy, "filed": filed}
    if start:
        f["start"] = start
    return f


def doc(concepts: dict[str, dict[str, list[dict[str, Any]]]], tax: str = "us-gaap") -> dict[str, Any]:
    return {"cik": 1, "entityName": "Test Co", "facts": {tax: {c: {"label": c, "units": u} for c, u in concepts.items()}}}


# one fiscal year Oct 2024 - Sep 2025 with quarterly reports (discrete income facts, YTD cash flow)
FY25 = ("2024-10-01", "2025-09-30")
Q = [("2024-10-01", "2024-12-31"), ("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30")]
REV = [fact(*FY25, 1000, fy=2025, filed="2025-11-01", accn="k25")] + [
    fact(s, e, v, form="10-Q", fp=f"Q{i + 1}", fy=2025, filed=f"2025-0{2 + 3 * i}-01", accn=f"q{i}")
    for i, ((s, e), v) in enumerate(zip(Q, (300, 200, 250)))]
OCF = [fact(*FY25, 400, accn="k25"),
       fact("2024-10-01", "2024-12-31", 90, form="10-Q", fp="Q1", accn="q0"),
       fact("2024-10-01", "2025-03-31", 170, form="10-Q", fp="Q2", accn="q1"),     # H1 (year-to-date)
       fact("2024-10-01", "2025-06-30", 300, form="10-Q", fp="Q3", accn="q2")]     # 9M (year-to-date)
EPS = [fact(*FY25, "4.10", accn="k25")] + [fact(s, e, v, form="10-Q", fp=f"Q{i + 1}", accn=f"q{i}")
                                         for i, ((s, e), v) in enumerate(zip(Q, ("1.20", "0.80", "1.00")))]


def base() -> dict[str, Any]:
    return doc({"Revenues": {"USD": REV}, "NetCashProvidedByUsedInOperatingActivities": {"USD": OCF},
                "EarningsPerShareDiluted": {"USD/shares": EPS},
                "Assets": {"USD": [fact(None, "2025-09-30", 5000), fact(None, "2025-06-30", 4800, form="10-Q", fp="Q3")]}})


def test_quarters_derived_q4_and_ytd_cash_flow() -> None:
    st = build(base())
    r, o, e = st.rows["revenue"], st.rows["operating_cash_flow"], st.rows["eps_diluted"]
    assert [p.key for p in st.quarterly] == ["FY2025Q4", "FY2025Q3", "FY2025Q2", "FY2025Q1"]
    assert r["FY2025Q1"].value == 300 and not r["FY2025Q1"].derived
    assert r["FY2025Q4"].value == 250 and r["FY2025Q4"].derived           # no 9M fact: FY - (Q1+Q2+Q3)
    assert r["FY2025Q4"].note == "derived: FY - (Q1+Q2+Q3)"
    assert (o["FY2025Q1"].value, o["FY2025Q2"].value, o["FY2025Q3"].value, o["FY2025Q4"].value) == (90, 80, 130, 100)
    assert o["FY2025Q2"].derived and o["FY2025Q2"].note == "derived: H1 - Q1"
    assert e["FY2025Q4"].value is None                                     # EPS is never subtracted
    assert st.rows["total_assets"]["FY2025Q3"].value == 4800               # balance item at the quarter end


def test_ttm_sum_of_four_consecutive_quarters() -> None:
    st = build(base())
    assert st.ttm is not None and st.ttm.end.isoformat() == "2025-09-30"
    assert st.rows["revenue"]["TTM"].value == 1000 and st.rows["operating_cash_flow"]["TTM"].value == 400
    assert st.rows["eps_diluted"]["TTM"].value is None and "not additive" in st.rows["eps_diluted"]["TTM"].note
    assert st.rows["total_assets"]["TTM"].value == 5000                   # latest value


def test_ttm_gap_gives_none_not_a_wrong_sum() -> None:
    d = base()
    d["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [f for f in REV if f["accn"] != "q1"]    # Q2 missing
    st = build(d)
    assert st.rows["revenue"]["FY2025Q2"].value is None
    assert st.rows["revenue"]["TTM"].value is None and st.rows["revenue"]["TTM"].note == "missing quarter: no TTM"


def test_latest_filing_wins_on_restatement() -> None:
    d = base()
    d["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = REV + [
        fact(*FY25, 1010, fy=2026, filed="2026-11-01", accn="k26")]        # restated in next year's 10-K
    st = build(d)
    cell = st.rows["revenue"]["FY2025"]
    assert cell.value == 1010 and cell.accn == "k26"
    assert st.annual[0].label == "FY2025, ended 2025-09-30"                # label from the smallest fy, not 2026


def test_fiscal_year_ending_in_january_keeps_its_own_label() -> None:
    d = doc({"Revenues": {"USD": [
        fact("2025-01-27", "2026-01-25", 500, fy=2026, filed="2026-02-25", accn="k26"),
        fact("2025-01-27", "2026-01-25", 500, fy=2027, filed="2027-02-25", accn="k27"),    # comparative
        fact("2024-01-29", "2025-01-26", 300, fy=2025, filed="2025-02-26", accn="k25")]}})
    st = build(d)
    assert [p.label for p in st.annual] == ["FY2026, ended 2026-01-25", "FY2025, ended 2025-01-26"]


def test_missing_concept_is_not_reported_never_zero() -> None:
    st = build(base())
    c = st.rows["capex"]["FY2025"]
    assert c.value is None and c.note == "not reported"
    assert st.rows["capex"]["TTM"].value is None


def test_concept_switch_fills_old_years_from_old_concept() -> None:
    d = doc({"SalesRevenueNet": {"USD": [fact("2022-10-01", "2023-09-30", 700, fy=2023, accn="k23")]},
             "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
                 fact("2023-10-01", "2024-09-30", 800, fy=2024, accn="k24")]}})
    st = build(d)
    r = st.rows["revenue"]
    assert r["FY2024"].value == 800 and r["FY2024"].concept.endswith("RevenueFromContractWithCustomerExcludingAssessedTax")
    assert r["FY2023"].value == 700 and r["FY2023"].concept == "us-gaap:SalesRevenueNet"


def test_newer_concept_wins_when_both_report_a_year() -> None:
    d = doc({"RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
                 fact("2023-10-01", "2024-09-30", 999, fy=2024, accn="old")]},
             "Revenues": {"USD": [fact("2023-10-01", "2024-09-30", 800, fy=2024, accn="k24"),
                                  fact("2024-10-01", "2025-09-30", 900, fy=2025, accn="k25")]}})
    cell = build(d).rows["revenue"]["FY2024"]
    assert cell.value == 800 and cell.concept == "us-gaap:Revenues"     # newer (lower-preference) concept wins


def test_other_forms_ignored_and_bad_rows_skipped() -> None:
    d = base()
    d["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = REV + [
        fact(*FY25, 5555, form="8-K", filed="2026-01-01", accn="x"), {"end": "bad", "val": 1, "form": "10-K", "filed": "2026-01-01"},
        fact(*FY25, None, filed="2026-02-01", accn="y")]
    assert build(d).rows["revenue"]["FY2025"].value == 1000
    assert len(read_facts(d, "us-gaap", "Revenues", "USD")) == len(REV)


def test_foreign_filer_currency_and_annual_only() -> None:
    d = doc({"Revenue": {"USD": [fact("2024-01-01", "2024-12-31", 90000000000, form="20-F", fy=2024, accn="usd")],
                         "TWD": [fact("2024-01-01", "2024-12-31", 2894307700000, form="20-F", fy=2024),
                                 fact("2023-01-01", "2023-12-31", 2161735800000, form="20-F", fy=2023,
                                      filed="2025-11-01", accn="k23")]},
             "DilutedEarningsLossPerShare": {"TWD/shares": [fact("2024-01-01", "2024-12-31", "44.67", form="20-F",
                                                                 fy=2024)]}}, tax="ifrs-full")
    assert reporting_currency(d) == "TWD"
    st = build(d)
    assert st.currency == "TWD" and st.quarterly == [] and st.taxonomy == "ifrs-full"
    assert st.rows["revenue"]["FY2024"].value == D("2894307700000")
    assert st.rows["revenue"]["TTM"].value == D("2894307700000") and "latest fiscal year" in st.rows["revenue"]["TTM"].note
    assert st.rows["eps_diluted"]["FY2024"].value == D("44.67")
    assert any("TWD" in w for w in st.warnings) and any("annual figures only" in w for w in st.warnings)


def test_split_flagged_not_adjusted() -> None:
    d = doc({"Revenues": {"USD": [fact("2023-10-01", "2024-09-30", 1, fy=2024, accn="k24"),
                                  fact("2024-10-01", "2025-09-30", 1, fy=2025, accn="k25")]},
             "WeightedAverageNumberOfDilutedSharesOutstanding": {"shares": [
                 fact("2023-10-01", "2024-09-30", 2_500_000_000, fy=2024, accn="k24"),
                 fact("2024-10-01", "2025-09-30", 25_000_000_000, fy=2025, accn="k25")]}})
    st = build(d)
    assert st.rows["shares_diluted"]["FY2025"].value == 25_000_000_000    # as reported
    assert st.rows["shares_diluted"]["FY2024"].value == 2_500_000_000     # older year not adjusted either
    flags = [w for w in st.warnings if "stock split" in w]
    assert len(flags) == 1 and "10.00x" in flags[0] and "not adjusted" in flags[0]


def test_real_apple_file_quarters_add_up_and_labels() -> None:
    d = json.loads((FIX / "edgar_aapl_trimmed.json").read_text(encoding="utf-8"))
    st = build(d, years=3, quarters=8)
    assert len(st.annual) == 3 and len(st.quarterly) == 8 and st.quarterly[0].label.startswith("Q4 FY2026")
    assert not any("stock split" in w for w in st.warnings)              # ~2% share changes are not flagged
    assert build(d, years=0, quarters=0).annual == []
    assert st.currency == "USD" and [p.label for p in st.annual][0].startswith("FY2026, ended 2026-09-")
    for line in ("revenue", "net_income", "operating_cash_flow"):
        row = st.rows[line]
        for fy in (2025, 2026):
            quarters = [row[f"FY{fy}Q{q}"].value for q in (1, 2, 3, 4)]
            assert None not in quarters and sum(quarters) == row[f"FY{fy}"].value, (line, fy)
    assert st.rows["revenue"]["FY2026"].concept == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert st.rows["eps_diluted"]["FY2026Q4"].value is None and st.rows["eps_diluted"]["FY2026Q1"].value is not None
    assert all(isinstance(c.value, Decimal) for c in st.rows["revenue"].values() if c.value is not None)


def test_quarter_length_fact_in_10k_is_used_directly() -> None:
    """Periods by length, not fp: a 3-month fact in a 10-K (fp=FY) is a real Q4, not something to derive."""
    d = base()
    u = d["facts"]["us-gaap"]
    u["Revenues"]["units"]["USD"] = REV + [fact("2025-07-01", "2025-09-30", 260, accn="k25")]
    u["EarningsPerShareDiluted"]["units"]["USD/shares"] = EPS + [fact("2025-07-01", "2025-09-30", "1.20", accn="k25")]
    st = build(d)
    assert st.rows["revenue"]["FY2025Q4"].value == 260 and not st.rows["revenue"]["FY2025Q4"].derived
    assert st.rows["eps_diluted"]["FY2025Q4"].value == D("1.20")


def test_ttm_crosses_fiscal_years() -> None:
    d = base()
    u = d["facts"]["us-gaap"]
    u["Revenues"]["units"]["USD"] = REV + [
        fact("2025-10-01", "2025-12-31", 350, form="10-Q", fp="Q1", fy=2026, filed="2026-02-01", accn="q26")]
    u["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"] = OCF + [
        fact("2025-10-01", "2025-12-31", 120, form="10-Q", fp="Q1", fy=2026, filed="2026-02-01", accn="q26")]
    st = build(d)
    assert st.ttm is not None and st.ttm.end.isoformat() == "2025-12-31"
    assert st.rows["revenue"]["TTM"].value == 1050                        # 200 + 250 + 250 (Q4) + 350
    assert st.rows["operating_cash_flow"]["TTM"].value == 430             # 80 + 130 + 100 + 120


def test_ttm_non_consecutive_quarters_refused() -> None:
    d = base()
    d["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = REV + [
        fact("2026-01-01", "2026-03-31", 400, form="10-Q", fp="Q2", fy=2026, filed="2026-05-01", accn="q2b")]
    d["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"] = list(OCF)
    st = build(d)
    assert st.rows["revenue"]["TTM"].value is None
    assert any("not consecutive" in w for w in st.warnings)


def test_early_january_year_end_uses_its_own_fy_not_end_year() -> None:
    st = build(doc({"Revenues": {"USD": [
        fact("2025-01-05", "2026-01-03", 500, fy=2025, filed="2026-02-20", accn="k25"),
        fact("2025-01-05", "2026-01-03", 500, fy=2026, filed="2027-02-20", accn="k26")]}}))     # comparative
    assert [p.label for p in st.annual] == ["FY2025, ended 2026-01-03"]


def test_labels_never_collide() -> None:
    """A later filing whose comparative carries a smaller fy must not give two years one label."""
    st = build(doc({"Revenues": {"USD": [
        fact("2023-01-01", "2023-12-31", 1, fy=2023, filed="2024-02-01", accn="k23"),
        fact("2024-01-01", "2024-12-31", 2, fy=2023, filed="2025-02-01", accn="odd"),   # mislabelled own year
        fact("2023-01-01", "2023-12-31", 1, fy=2023, filed="2025-02-01", accn="odd")]}}))
    labels = [p.key for p in st.annual]
    assert len(labels) == len(set(labels)) == 2
    assert st.rows["revenue"][labels[0]].value == 2 and st.rows["revenue"][labels[1]].value == 1


def test_year_in_progress_quarters_only() -> None:
    d = base()
    u = d["facts"]["us-gaap"]
    u["Revenues"]["units"]["USD"] = REV + [
        fact("2025-10-01", "2025-12-31", 400, form="10-Q", fp="Q1", fy=2026, filed="2026-02-01", accn="n0"),
        fact("2026-01-01", "2026-03-31", 350, form="10-Q", fp="Q2", fy=2026, filed="2026-05-01", accn="n1")]
    u["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"] = OCF + [
        fact("2025-10-01", "2025-12-31", 120, form="10-Q", fp="Q1", fy=2026, filed="2026-02-01", accn="n0"),
        fact("2025-10-01", "2026-03-31", 200, form="10-Q", fp="Q2", fy=2026, filed="2026-05-01", accn="n1")]
    st = build(d)
    keys = [p.key for p in st.quarterly]
    assert keys[:2] == ["FY2026Q2", "FY2026Q1"] and "FY2026Q4" not in keys
    assert "FY2026" not in [p.key for p in st.annual]                     # no annual report yet
    assert st.rows["operating_cash_flow"]["FY2026Q2"].value == 80 and st.rows["operating_cash_flow"]["FY2026Q2"].derived


def test_derived_quarter_uses_one_vintage() -> None:
    """FY restated in next year's 10-K: Q4 = FY (as first reported) - 9M, not restated FY - unrestated 9M."""
    d = base()
    d["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"] = OCF + [
        fact(*FY25, 450, fy=2026, filed="2026-11-01", accn="k26")]        # FY later restated 400 -> 450
    st = build(d)
    assert st.rows["operating_cash_flow"]["FY2025"].value == 450          # the year shows the restated figure
    q4 = st.rows["operating_cash_flow"]["FY2025Q4"]
    assert q4.value == 100 and "first reported" in q4.note                # 400 - 300, same vintage


def test_currency_from_newest_report_after_a_switch() -> None:
    old = [fact(f"{y}-01-01", f"{y}-12-31", 10, form="20-F", fy=y, filed=f"{y + 1}-04-01", accn=f"e{y}")
           for y in range(2015, 2022)]
    new = [fact(f"{y}-01-01", f"{y}-12-31", 20, form="20-F", fy=y, filed=f"{y + 1}-04-01", accn=f"u{y}")
           for y in range(2022, 2025)]
    d = doc({"Revenue": {"EUR": old, "USD": new}}, tax="ifrs-full")
    assert reporting_currency(d) == "USD"
    assert build(d).rows["revenue"]["FY2024"].value == 20


def test_quarterly_reports_that_stopped_make_no_old_ttm() -> None:
    q = [fact("2019-01-01", "2019-03-31", 5, form="10-Q", fp="Q1", fy=2019, filed="2019-05-01", accn="q19")]
    years = [fact(f"{y}-01-01", f"{y}-12-31", 100 + y - 2019, form="20-F" if y > 2019 else "10-K", fy=y,
                  filed=f"{y + 1}-03-01", accn=f"a{y}") for y in range(2019, 2025)]
    st = build(doc({"Revenues": {"USD": q + years}}))
    assert st.rows["revenue"]["TTM"].value == 105 and "latest fiscal year" in st.rows["revenue"]["TTM"].note
    assert any("Quarterly reports stop" in w for w in st.warnings)
