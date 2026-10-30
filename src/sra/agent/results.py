"""Summarise a company's latest quarterly results, rendered in code.

Compares the latest stored quarter with the one before it (QoQ) and with
the same quarter a year earlier (YoY), from Upstox's quarterly income
statement rows.
"""

from datetime import date
from decimal import Decimal

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.agent.universe import Series, load
from sra.figures import months_between, pct_change, period_label

CATEGORIES = (
    ("revenue", "Revenue"),
    ("operating_profit", "Operating profit"),
    ("net_profit", "Net profit"),
)

NOT_FOUND = 'Name one company, e.g. "TCS latest quarterly results".'


def _value_at(series: Series, when: date) -> Decimal | None:
    return next((value for day, value in series if day == when), None)


def _change(prior: Decimal | None, current: Decimal) -> str:
    if prior is None or prior <= 0 or current <= 0:
        return "n/a"
    return f"{pct_change(prior, current):+}%"


def answer(question: str) -> PathResult:
    found = resolve_companies(question)
    if len(found.companies) != 1:
        return PathResult(text=NOT_FOUND)
    company = found.companies[0]
    facts = load([company.instrument_key], quarterly=True).companies.get(
        company.instrument_key
    )
    revenue = facts.quarterly.get("revenue", []) if facts else []
    if not facts or not revenue:
        return PathResult(
            text=f"{company.name} ({company.ticker}): no quarterly results are stored."
        )

    latest = revenue[-1][0]
    previous = next(
        (day for day, _ in reversed(revenue) if months_between(day, latest) == 3), None
    )
    year_ago = next(
        (day for day, _ in revenue if months_between(day, latest) == 12), None
    )
    rows = []
    for category, label in CATEGORIES:
        series = facts.quarterly.get(category, [])
        current = _value_at(series, latest)
        if current is None:
            continue
        qoq = _change(_value_at(series, previous) if previous else None, current)
        yoy = _change(_value_at(series, year_ago) if year_ago else None, current)
        rows.append(f"  {label}: ₹{current:,.2f} cr (QoQ {qoq}, YoY {yoy})")

    net = _value_at(facts.quarterly.get("net_profit", []), latest)
    sales = _value_at(revenue, latest)
    margin_lines = []
    if net is not None and sales and sales > 0:
        margin_lines.append(f"  Net margin: {100 * net / sales:.1f}%")
        if year_ago:
            old_net = _value_at(facts.quarterly.get("net_profit", []), year_ago)
            old_sales = _value_at(revenue, year_ago)
            if old_net is not None and old_sales and old_sales > 0:
                margin_lines.append(
                    f"  A year earlier: {100 * old_net / old_sales:.1f}%"
                )

    compared = [
        f"QoQ is against {period_label(previous)}" if previous else "",
        f"YoY against {period_label(year_ago)}" if year_ago else "",
    ]
    note = "; ".join(part for part in compared if part) or "No earlier quarter stored."
    sections = [
        f"{company.name} ({company.ticker}): quarter to {period_label(latest)}\n"
        f"Consolidated figures from Upstox. {note}.",
        "Results\n" + "\n".join(rows),
    ]
    if margin_lines:
        sections.append("Margin\n" + "\n".join(margin_lines))
    sections.append(
        "Not covered here: management commentary and guidance. Ask about the "
        "company's earnings call for those."
    )
    return PathResult(text="\n\n".join(sections))
