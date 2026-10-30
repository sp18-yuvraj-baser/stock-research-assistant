"""Compare two to five companies side by side, rendered in code.

Each company is found in its own part of the question ("TCS vs Infosys",
"HDFC Bank, ICICI Bank and SBI"), so a weak name next to a strong one is not
lost. Every figure comes from stored fundamentals or the live quote; the
answer lines figures up and never names a winner.
"""

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from functools import partial
from itertools import pairwise

from sra.agent.companies import resolve_companies
from sra.agent.history import price_stats
from sra.agent.loop import PathResult, fundamentals_path, general_finance_path
from sra.agent.universe import CompanyFacts, Series, load
from sra.figures import cagr, pct_change, period_label, usable_benchmark
from sra.upstox.client import UpstoxError
from sra.upstox.instruments import EquityInstrument
from sra.upstox.market import daily_candles, full_quotes

MAX_COMPANIES = 5
GROWTH_YEARS = 3
_SEPARATORS = r"\b(?:vs\.?|versus|and|with|or|against)\b|[,/&]"

DISCLAIMER = (
    "This is research, not investment advice. For a personal recommendation, "
    "consult a SEBI-registered investment adviser."
)


def companies_in(question: str) -> list[EquityInstrument]:
    """Distinct companies, one per separated part, in the order named."""
    found: list[EquityInstrument] = []
    for part in re.split(_SEPARATORS, question, flags=re.IGNORECASE):
        if not part or not part.strip():
            continue
        resolution = resolve_companies(part)
        if resolution.companies and resolution.companies[0] not in found:
            found.append(resolution.companies[0])
    return found[:MAX_COMPANIES]


# A single-year jump beyond this is a merger or one-off, not steady growth.
STEP_CHANGE_PCT = Decimal(50)


def _growth(series: Series, years: int) -> str:
    """Compound growth over `years`, flagged when one year jumped by a merger
    or one-off rather than grew."""
    if len(series) < years + 1:
        return "n/a"
    span = series[-(years + 1) :]
    rate = cagr(span[0][1], span[-1][1], years)
    if rate is None:
        return "n/a"
    jumped = any(
        prior > 0 and pct_change(prior, current) > STEP_CHANGE_PCT
        for (_, prior), (_, current) in pairwise(span)
    )
    return f"{rate}%" + (" (includes a one-year jump over 50%)" if jumped else "")


def _latest(series: Series | None) -> tuple[date, Decimal] | None:
    return series[-1] if series else None


def _fmt(value: Decimal | float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    number = Decimal(str(value)).normalize()
    return f"{number:,f}{suffix}"


def _ratio_cell(company: CompanyFacts, *, name: str, suffix: str = "") -> str:
    own, sector = company.ratios.get(name, (None, None))
    if own is None:
        return "n/a"
    text = _fmt(own, suffix)
    if sector is None:
        return text
    if usable_benchmark(own, sector) is None and name != "Net NPA":
        return f"{text} (sector figure unreliable)"
    return f"{text} (sector {_fmt(sector, suffix)})"


def _margin(company: CompanyFacts) -> str:
    revenue, profit = (
        _latest(company.yearly.get("revenue")),
        _latest(company.yearly.get("net_profit")),
    )
    if not revenue or not profit or revenue[0] != profit[0] or revenue[1] <= 0:
        return "n/a"
    return f"{100 * profit[1] / revenue[1]:.1f}% ({period_label(revenue[0])})"


def _leverage(company: CompanyFacts) -> str:
    if company.is_bank:
        return "n/a for banks"
    assets, liabilities = (
        _latest(company.yearly.get("total_asset")),
        _latest(company.yearly.get("total_liability")),
    )
    if not assets or not liabilities or assets[1] <= 0:
        return "n/a"
    return f"{100 * liabilities[1] / assets[1]:.0f}%"


def _cash_conversion(company: CompanyFacts) -> str:
    operating, profit = (
        _latest(company.yearly.get("operating")),
        _latest(company.yearly.get("net_profit")),
    )
    if not operating:
        return "n/a"
    text = f"₹{operating[1]:,.0f} cr"
    if profit and profit[0] == operating[0] and profit[1] > 0:
        text += f" ({operating[1] / profit[1]:.1f}x net profit)"
    return text


def _year_return(key: str, price: float | None, today: date) -> str:
    if price is None:
        return "n/a"
    try:
        stats = price_stats(daily_candles(key, years=1, today=today), price, today)
    except UpstoxError:
        return "n/a"
    if stats is None:
        return "n/a"
    total = next((t for label, t, _ in stats.returns if label == "1 year"), None)
    return "n/a" if total is None else f"{total:+.1f}%"


def answer(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    named = companies_in(question)
    if len(named) < 2:
        # "compare stocks and mutual funds" is a concept, and one company is
        # a fundamentals question; both are better served by those paths.
        path = fundamentals_path if named else general_finance_path
        return path(question)

    facts = load([c.instrument_key for c in named]).companies
    try:
        quotes = full_quotes([c.instrument_key for c in named])
    except UpstoxError:
        quotes = {}
    rows: list[CompanyFacts] = []
    missing: list[str] = []
    for company in named:
        if company.instrument_key in facts:
            rows.append(facts[company.instrument_key])
        else:
            missing.append(company.ticker)
    if len(rows) < 2:
        return PathResult(
            text="Not enough stored fundamentals to compare "
            + ", ".join(c.ticker for c in named)
            + ". Run `sra upstox ingest-fundamentals` for them first."
        )

    price = {
        c.key: (quotes[c.key].last_price if c.key in quotes else None) for c in rows
    }

    def row(label: str, render: Callable[[CompanyFacts], str]) -> str:
        return "  " + label + ": " + " · ".join(f"{c.ticker} {render(c)}" for c in rows)

    def ratio_rows(names: tuple[str, ...], suffix: str = "") -> str:
        return "\n".join(
            row(n, partial(_ratio_cell, name=n, suffix=suffix)) for n in names
        )

    def growth(category: str) -> Callable[[CompanyFacts], str]:
        return lambda c: _growth(c.yearly.get(category, []), GROWTH_YEARS)

    def live(c: CompanyFacts) -> str:
        return _fmt(price[c.key], " INR")

    def year(c: CompanyFacts) -> str:
        return _year_return(c.key, price[c.key], today)

    sections = [
        "Comparison: "
        + " vs ".join(c.ticker for c in rows)
        + "\n"
        + " · ".join(f"{c.ticker} = {c.name}" for c in rows),
        "Price\n" + row("Live price", live) + "\n" + row("1-year return", year),
        "Valuation\n" + ratio_rows(("P/E", "P/B", "EV/EBITDA")),
        "Quality\n" + ratio_rows(("ROE", "ROCE", "ROA"), "%"),
        f"Growth over {GROWTH_YEARS} years (a year, compounded)\n"
        + row("Revenue", growth("revenue"))
        + "\n"
        + row("Net profit", growth("net_profit"))
        + "\n"
        + row("Net margin", _margin),
        "Balance sheet and cash\n"
        + row("Liabilities / assets", _leverage)
        + "\n"
        + row("Cash from operations", _cash_conversion),
    ]
    if any(c.is_bank for c in rows):
        sections.append("Bank measures\n" + ratio_rows(("NIM", "Net NPA", "CASA"), "%"))
    notes = [
        "Debt-to-equity is not in this data; liabilities / assets is shown instead."
    ]
    if missing:
        notes.append(f"No stored fundamentals for {', '.join(missing)}, so left out.")
    sections.append("Notes\n" + "\n".join(f"  - {n}" for n in notes))
    sections.append(DISCLAIMER)
    return PathResult(text="\n\n".join(sections))
