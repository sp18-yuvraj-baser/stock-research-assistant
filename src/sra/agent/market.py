"""Answer market-wide questions: index levels, top movers, open/holidays.

Rendered in code from one batched Upstox quote call per answer. Movers are
ranked among established companies (yearly revenue of at least ₹5,000 crore)
so a thinly traded penny stock cannot top the list.
"""

import re
from datetime import date

import psycopg

from sra.agent.loop import PathResult
from sra.db import connect
from sra.upstox.client import UpstoxError
from sra.upstox.market import (
    INDICES,
    FullQuote,
    find_index,
    full_quotes,
    market_status,
    upcoming_holidays,
)

MOVERS_MIN_REVENUE_CRORE = 5000
MOVERS_SHOWN = 5

UNIVERSE_SQL = """
SELECT DISTINCT i.instrument_key, i.ticker, i.name
FROM instruments i
JOIN fundamental_financials f ON f.instrument_key = i.instrument_key
WHERE i.exchange = 'NSE_EQ' AND f.time_period = 'yearly'
  AND f.category = 'revenue' AND f.value >= %(min_revenue)s
"""

_MOVERS = (
    r"\b(gainers?|losers?|most active|top movers|movers"
    r"|biggest (rise|fall|gain|loss))\b"
)
_STATUS = (
    r"\b(is (the )?market (open|closed)|market (hours|timings)|holidays?|open today)\b"
)


def _status_words(status: str | None) -> str:
    if not status:
        return "status unknown"
    if "PRE_OPEN" in status:
        return "in the pre-open session"
    if status.endswith("OPEN"):
        return "open"
    return "closed"


def _change(quote: FullQuote) -> str:
    if quote.change_pct is None or quote.net_change is None:
        return f"{quote.last_price:,.2f}"
    return (
        f"{quote.last_price:,.2f} ({quote.net_change:+,.2f}, {quote.change_pct:+.2f}%)"
    )


def _status_block(today: date) -> str:
    status = _status_words(market_status("NSE"))
    lines = [f"  NSE is {status} now."]
    holidays = upcoming_holidays(today)
    if holidays:
        lines += [f"  Holiday: {day:%a %d %b %Y}, {name}" for day, name in holidays]
    return "Market status\n" + "\n".join(lines)


def _overview(question: str, today: date) -> str:
    named = find_index(question)
    ordered = list(INDICES)
    if named is not None:
        ordered.remove(named)
        ordered.insert(0, named)
    quotes = full_quotes([index.key for index in ordered])
    rows = [
        f"  {index.name}: {_change(quotes[index.key])}"
        for index in ordered
        if index.key in quotes
    ]
    if not rows:
        return "Upstox returned no index levels right now."
    title = f"{named.name} and the wider market" if named else "Market today"
    return "\n\n".join(
        [
            f"{title}\nIndex levels from Upstox, change from the previous close.",
            "Indices\n" + "\n".join(rows),
            _status_block(today),
        ]
    )


def _universe() -> dict[str, tuple[str, str]]:
    with connect(readonly=True) as conn:
        rows = conn.execute(
            UNIVERSE_SQL, {"min_revenue": MOVERS_MIN_REVENUE_CRORE}
        ).fetchall()
    return {str(r["instrument_key"]): (str(r["ticker"]), str(r["name"])) for r in rows}


def _movers() -> str:
    universe = _universe()
    if not universe:
        return "No company fundamentals are stored yet to pick the movers from."
    quotes = [
        q for q in full_quotes(list(universe)).values() if q.change_pct is not None
    ]
    if not quotes:
        return "Upstox returned no quotes for the movers right now."

    def row(quote: FullQuote) -> str:
        ticker, name = universe[quote.key]
        return f"  {ticker} ({name}): {_change(quote)}"

    by_change = sorted(quotes, key=lambda q: q.change_pct or 0.0)
    active = sorted(
        (q for q in quotes if q.traded_value), key=lambda q: -(q.traded_value or 0.0)
    )
    sections = [
        f"Top movers today\nAmong {len(quotes)} established NSE companies "
        f"(yearly revenue of at least ₹{MOVERS_MIN_REVENUE_CRORE:,} crore).",
        "Gainers\n" + "\n".join(row(q) for q in by_change[::-1][:MOVERS_SHOWN]),
        "Losers\n" + "\n".join(row(q) for q in by_change[:MOVERS_SHOWN]),
    ]
    if active:
        sections.append(
            "Most active by value traded\n"
            + "\n".join(
                f"{row(q)}, ₹{(q.traded_value or 0) / 1e7:,.0f} crore traded"
                for q in active[:MOVERS_SHOWN]
            )
        )
    sections.append("A day's move says nothing about a company's long-term value.")
    return "\n\n".join(sections)


def answer(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    try:
        if re.search(_MOVERS, question, re.IGNORECASE):
            text = _movers()
        elif re.search(_STATUS, question, re.IGNORECASE):
            text = "Market status\n" + _status_block(today).split("\n", 1)[1]
        else:
            text = _overview(question, today)
    except UpstoxError as exc:
        text = f"Market data is unavailable right now ({exc})."
    except psycopg.Error as exc:
        text = f"Could not read the company list right now ({str(exc).strip()})."
    return PathResult(text=text)
