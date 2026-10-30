"""Answer price-history questions: 52-week range, returns over time, charts.

Rendered in code from Upstox daily candles and the live quote. Returns are
price only (dividends excluded) and every percentage is computed here.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sra.agent.companies import resolve_companies
from sra.agent.loop import Chart, PathResult
from sra.figures import rupees
from sra.upstox.client import UpstoxError
from sra.upstox.market import Candle, daily_candles, find_index, full_quotes

# Periods reported in every answer: (label, days back, years for CAGR).
PERIODS = (
    ("1 month", 30, None),
    ("6 months", 182, None),
    ("1 year", 365, None),
    ("3 years", 3 * 365, 3),
    ("5 years", 5 * 365, 5),
)
_WORDS = {"one": 1, "two": 2, "three": 3, "five": 5, "six": 6, "ten": 10}
MAX_CHART_POINTS = 260
MAX_YEARS = 10

NOT_FOUND = (
    'Name a company or index, e.g. "TCS 1 year return", "chart of Reliance '
    'over 5 years" or "Nifty 50 52 week high".'
)


@dataclass(frozen=True)
class Extreme:
    price: float
    day: date


@dataclass(frozen=True)
class PriceStats:
    price: float
    high: Extreme
    low: Extreme
    returns: list[tuple[str, float | None, float | None]]  # label, total %, %/yr


def _close_on_or_before(candles: list[Candle], day: date) -> float | None:
    earlier = [candle for candle in candles if candle.day <= day]
    return earlier[-1].close if earlier else None


def price_stats(candles: list[Candle], price: float, today: date) -> PriceStats | None:
    """52-week extremes and period returns ending at `price` today."""
    year = [c for c in candles if c.day > today - timedelta(days=365)]
    if not year:
        return None
    highest = max(year, key=lambda c: c.high)
    lowest = min(year, key=lambda c: c.low)
    high = Extreme(highest.high, highest.day)
    low = Extreme(lowest.low, lowest.day)
    if price > high.price:
        high = Extreme(price, today)
    if price < low.price:
        low = Extreme(price, today)
    returns: list[tuple[str, float | None, float | None]] = []
    for label, days, years in PERIODS:
        start = _close_on_or_before(candles, today - timedelta(days=days))
        if start is None or start <= 0 or candles[0].day > today - timedelta(days=days):
            returns.append((label, None, None))
            continue
        total = 100 * (price / start - 1)
        per_year = 100 * ((price / start) ** (1 / years) - 1) if years else None
        returns.append((label, total, per_year))
    return PriceStats(price, high, low, returns)


def requested_years(question: str) -> float:
    """The span a question asks to chart, in years (default one, capped)."""
    match = re.search(
        r"\b(\d+|one|two|three|five|six|ten) ?(years?|yrs?|y|months?|m)\b",
        question,
        re.IGNORECASE,
    )
    if not match:
        return 1.0
    amount = _WORDS.get(match.group(1).lower()) or int(match.group(1))
    years = amount / 12 if match.group(2).lower().startswith("m") else float(amount)
    return min(max(years, 1 / 12), MAX_YEARS)


def chart_points(candles: list[Candle], since: date) -> list[tuple[str, float]]:
    """Closes from `since`, thinned evenly to at most MAX_CHART_POINTS."""
    span = [c for c in candles if c.day >= since]
    if len(span) > MAX_CHART_POINTS:
        step = len(span) / MAX_CHART_POINTS
        picked = [span[int(i * step)] for i in range(MAX_CHART_POINTS - 1)]
        span = [*picked, span[-1]]
    return [(c.day.isoformat(), c.close) for c in span]


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _target(question: str) -> tuple[str, str, str] | None:
    """(instrument key, display name, short name) for the index or company."""
    index = find_index(question)
    if index is not None:
        return index.key, index.name, index.name
    found = resolve_companies(question)
    if len(found.companies) != 1:
        return None
    company = found.companies[0]
    return company.instrument_key, f"{company.name} ({company.ticker})", company.ticker


def answer(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    target = _target(question)
    if target is None:
        return PathResult(text=NOT_FOUND)
    key, title, short = target
    years = requested_years(question)
    try:
        candles = daily_candles(key, years=max(5, int(years + 0.999)), today=today)
        quote = full_quotes([key]).get(key)
    except UpstoxError as exc:
        return PathResult(
            text=f"{title}: price history is unavailable right now ({exc})."
        )
    if not candles:
        return PathResult(text=f"{title}: Upstox returned no price history.")
    price = quote.last_price if quote else candles[-1].close
    stats = price_stats(candles, price, today)
    if stats is None:
        return PathResult(
            text=f"{title}: not enough price history for a 52-week range."
        )

    headline = f"Price {rupees(price)}"
    if quote is not None and quote.change_pct is not None:
        headline += f", {_pct(quote.change_pct)} today"
    elif quote is None:
        headline += f" (last close, {candles[-1].day:%d %b %Y})"
    lines = [
        f"{title}: price history\n{headline}",
        "52-week range\n"
        f"  High: {rupees(stats.high.price)} on {stats.high.day:%d %b %Y} "
        f"(price is {100 * (1 - price / stats.high.price):.1f}% below it)\n"
        f"  Low: {rupees(stats.low.price)} on {stats.low.day:%d %b %Y} "
        f"(price is {100 * (price / stats.low.price - 1):.1f}% above it)",
    ]
    rows = []
    for label, total, per_year in stats.returns:
        if total is None:
            rows.append(f"  {label}: not enough history")
        elif per_year is not None:
            rows.append(f"  {label}: {_pct(total)} ({_pct(per_year)} a year)")
        else:
            rows.append(f"  {label}: {_pct(total)}")
    lines.append("Price returns (dividends excluded)\n" + "\n".join(rows))
    lines.append("Past returns do not predict future returns.")
    span_label = (
        f"{years:g} year{'s' if years != 1 else ''}"
        if years >= 1
        else (f"{round(years * 12)} months")
    )
    since = today - timedelta(days=int(365 * years))
    return PathResult(
        text="\n\n".join(lines),
        chart=Chart(f"{short}, {span_label}", chart_points(candles, since)),
    )
