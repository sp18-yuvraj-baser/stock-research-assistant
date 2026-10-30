"""Explain a metric with a company's own numbers, rendered in code.

"How is TCS's ROE calculated?" walks through the formula with stored yearly
figures and sets the result beside Upstox's own ratio, saying why the two can
differ. Without a company, the general explainer answers instead.
"""

import re
from decimal import Decimal

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult, general_finance_path
from sra.agent.universe import CompanyFacts, load
from sra.figures import period_label, rupees
from sra.upstox.client import UpstoxError
from sra.upstox.market import full_quotes

_METRIC = (
    ("ROE", r"\broe\b|return on equity"),
    ("ROA", r"\broa\b|return on assets"),
    ("P/E", r"\bp/?e\b|price.to.earnings"),
    ("P/B", r"\bp/?b\b|price.to.book"),
    ("Net margin", r"\b(net |profit )?margin\b"),
)


def _latest(company: CompanyFacts, category: str) -> tuple[str, Decimal] | None:
    series = company.yearly.get(category)
    return (period_label(series[-1][0]), series[-1][1]) if series else None


def _upstox(company: CompanyFacts, name: str) -> str:
    value = company.ratio(name)
    return f"{value.normalize():f}" if value is not None else "not available"


def _roe_roa(company: CompanyFacts, metric: str) -> str:
    profit = _latest(company, "net_profit")
    assets = _latest(company, "total_asset")
    liabilities = _latest(company, "total_liability")
    if not profit or not assets or (metric == "ROE" and not liabilities):
        return "The stored yearly figures needed for this are missing."
    year = profit[0]
    if metric == "ROA":
        value = 100 * profit[1] / assets[1]
        return (
            "Formula\n  ROA = net profit / total assets\n\n"
            f"With {company.ticker}'s {year} figures (INR crore)\n"
            f"  Net profit: {profit[1]:,.2f}\n  Total assets: {assets[1]:,.2f}\n"
            f"  ROA = {profit[1]:,.2f} / {assets[1]:,.2f} = {value:.2f}%\n\n"
            f"Upstox's ROA: {_upstox(company, 'ROA')}%"
        )
    assert liabilities is not None
    equity = assets[1] - liabilities[1]
    if equity <= 0:
        return (
            f"{company.ticker}'s liabilities exceed its assets in {year}, so "
            "shareholders' equity is negative and ROE is not meaningful."
        )
    value = 100 * profit[1] / equity
    return (
        "Formula\n  ROE = net profit / shareholders' equity\n"
        "  Shareholders' equity = total assets - total liabilities\n\n"
        f"With {company.ticker}'s {year} figures (INR crore)\n"
        f"  Net profit: {profit[1]:,.2f}\n  Total assets: {assets[1]:,.2f}\n"
        f"  Total liabilities: {liabilities[1]:,.2f}\n"
        f"  Equity = {assets[1]:,.2f} - {liabilities[1]:,.2f} = {equity:,.2f}\n"
        f"  ROE = {profit[1]:,.2f} / {equity:,.2f} = {value:.2f}%\n\n"
        f"Upstox's ROE: {_upstox(company, 'ROE')}%\n"
        "  The two can differ: providers often use average equity over the "
        "year, profit attributable to shareholders only, or a different period."
    )


def _price_multiple(company: CompanyFacts, metric: str, price: float | None) -> str:
    ratio = company.ratio(metric)
    if ratio is None or ratio <= 0:
        return f"Upstox has no positive {metric} for {company.ticker}."
    if price is None:
        return (
            f"{metric}: {ratio.normalize():f} (live price unavailable for the "
            "worked example)."
        )
    live = Decimal(str(price))
    per_share = live / ratio
    if metric == "P/E":
        return (
            "Formula\n  P/E = share price / earnings per share (EPS)\n\n"
            f"With {company.ticker}'s numbers\n  Price: {rupees(live)} (live)\n"
            f"  Upstox P/E: {ratio.normalize():f}\n"
            f"  So the EPS behind it is about {rupees(live)} / {ratio.normalize():f}"
            f" = {rupees(per_share)} a share\n\n"
            f"Reading it: buyers pay about ₹{ratio:.0f} for each ₹1 of yearly "
            "earnings. EPS and share count are not stored here, so the EPS is "
            "implied from the ratio, not reported."
        )
    return (
        "Formula\n  P/B = share price / book value per share\n\n"
        f"With {company.ticker}'s numbers\n  Price: {rupees(live)} (live)\n"
        f"  Upstox P/B: {ratio.normalize():f}\n"
        f"  So book value is about {rupees(per_share)} a share\n\n"
        f"Reading it: the market values each ₹1 of net assets at about "
        f"₹{ratio:.2f}."
    )


def _margin(company: CompanyFacts) -> str:
    profit, revenue = _latest(company, "net_profit"), _latest(company, "revenue")
    if not profit or not revenue or revenue[1] <= 0:
        return "The stored yearly figures needed for this are missing."
    return (
        "Formula\n  Net margin = net profit / revenue\n\n"
        f"With {company.ticker}'s {revenue[0]} figures (INR crore)\n"
        f"  Net profit: {profit[1]:,.2f}\n  Revenue: {revenue[1]:,.2f}\n"
        f"  Net margin = {100 * profit[1] / revenue[1]:.2f}%"
    )


def answer(question: str) -> PathResult:
    metric = next(
        (name for name, pattern in _METRIC if re.search(pattern, question, re.I)),
        None,
    )
    found = resolve_companies(question)
    if metric is None or len(found.companies) != 1:
        return general_finance_path(question)
    listing = found.companies[0]
    company = load([listing.instrument_key]).companies.get(listing.instrument_key)
    if company is None:
        return PathResult(text=f"No stored fundamentals for {listing.ticker}.")
    if metric in ("ROE", "ROA"):
        body = _roe_roa(company, metric)
    elif metric == "Net margin":
        body = _margin(company)
    else:
        try:
            quote = full_quotes([listing.instrument_key]).get(listing.instrument_key)
        except UpstoxError:
            quote = None
        body = _price_multiple(company, metric, quote.last_price if quote else None)
    return PathResult(
        text=f"{company.name} ({company.ticker}): how {metric} is worked out\n\n{body}"
    )
