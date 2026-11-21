"""Statement line items -> XBRL concept names (phase 6 part B), with ordered fallbacks.

Names are standard us-gaap / ifrs-full element names, checked against real SEC companyfacts files (AAPL, NVDA,
TSM, 2026-11-21). Companies switch concepts over the years (Apple: SalesRevenueNet -> RevenueFrom...; NVIDIA:
RevenueFrom... -> Revenues), so statements.py ranks the candidates present in a company's file by their most recent
period and fills each period from the first candidate that reports it; every value records which concept it used.
A line with no candidate in the file is "not reported" (None), never 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Statement = Literal["income", "balance", "cashflow"]
Measure = Literal["money", "per_share", "shares"]


@dataclass(frozen=True)
class Line:
    id: str
    label: str
    statement: Statement
    instant: bool                  # balance-sheet style (value at a date) vs flow over a period
    measure: Measure
    candidates: tuple[tuple[str, str], ...]   # (taxonomy, concept) in preference order

    @property
    def derivable(self) -> bool:
        """Quarter values may be derived by subtraction (Q4 = FY - 9M, Q2 = H1 - Q1) only for money flows.
        Per-share values and share counts are not additive."""
        return not self.instant and self.measure == "money"


def _g(*names: str) -> tuple[tuple[str, str], ...]:
    return tuple(("us-gaap", n) for n in names)


def _i(*names: str) -> tuple[tuple[str, str], ...]:
    return tuple(("ifrs-full", n) for n in names)


LINES: tuple[Line, ...] = (
    # income statement (flows)
    Line("revenue", "Revenue", "income", False, "money",
         _g("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax") + _i("Revenue")),
    Line("cost_of_revenue", "Cost of revenue", "income", False, "money",
         _g("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold") + _i("CostOfSales")),
    Line("gross_profit", "Gross profit", "income", False, "money", _g("GrossProfit") + _i("GrossProfit")),
    Line("rnd", "Research & development", "income", False, "money",
         _g("ResearchAndDevelopmentExpense") + _i("ResearchAndDevelopmentExpense")),
    Line("operating_income", "Operating income", "income", False, "money",
         _g("OperatingIncomeLoss") + _i("ProfitLossFromOperatingActivities")),
    Line("net_income", "Net income", "income", False, "money",
         _g("NetIncomeLoss", "ProfitLoss") + _i("ProfitLossAttributableToOwnersOfParent", "ProfitLoss")),
    Line("eps_diluted", "EPS (diluted)", "income", False, "per_share",
         _g("EarningsPerShareDiluted") + _i("DilutedEarningsLossPerShare")),
    Line("eps_basic", "EPS (basic)", "income", False, "per_share",
         _g("EarningsPerShareBasic") + _i("BasicEarningsLossPerShare")),
    Line("shares_diluted", "Diluted shares (weighted avg.)", "income", False, "shares",
         _g("WeightedAverageNumberOfDilutedSharesOutstanding") + _i("AdjustedWeightedAverageShares")),
    # balance sheet (values at a date)
    Line("cash", "Cash & equivalents", "balance", True, "money",
         _g("CashAndCashEquivalentsAtCarryingValue") + _i("CashAndCashEquivalents")),
    Line("total_assets", "Total assets", "balance", True, "money", _g("Assets") + _i("Assets")),
    Line("total_liabilities", "Total liabilities", "balance", True, "money", _g("Liabilities") + _i("Liabilities")),
    Line("equity", "Shareholders' equity", "balance", True, "money",
         _g("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
         + _i("EquityAttributableToOwnersOfParent", "Equity")),
    Line("long_term_debt", "Long-term debt", "balance", True, "money",
         _g("LongTermDebtNoncurrent", "LongTermDebt") + _i("NoncurrentPortionOfNoncurrentBorrowings")),
    Line("shares_outstanding", "Shares outstanding", "balance", True, "shares",
         _g("CommonStockSharesOutstanding") + (("dei", "EntityCommonStockSharesOutstanding"),)),
    # cash flow (flows; 10-Q reports them year-to-date)
    Line("operating_cash_flow", "Operating cash flow", "cashflow", False, "money",
         _g("NetCashProvidedByUsedInOperatingActivities") + _i("CashFlowsFromUsedInOperatingActivities")),
    Line("capex", "Capital expenditure", "cashflow", False, "money",
         _g("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
         + _i("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")),
    Line("buybacks", "Share buybacks", "cashflow", False, "money",
         _g("PaymentsForRepurchaseOfCommonStock") + _i("PaymentsToAcquireOrRedeemEntitysShares")),
    Line("dividends_paid", "Dividends paid", "cashflow", False, "money",
         _g("PaymentsOfDividends", "PaymentsOfDividendsCommonStock")
         + _i("DividendsPaidClassifiedAsFinancingActivities")),
)

BY_ID = {line.id: line for line in LINES}
