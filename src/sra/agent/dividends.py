"""Answer dividend questions from Upstox's corporate actions, live.

Upstox returns about the last year of events, which is exactly what a
trailing-twelve-month yield needs: the sum of dividends that went ex in the
last 365 days, divided by today's price.
"""

from datetime import date, timedelta
from decimal import Decimal

import psycopg

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.db import connect
from sra.figures import parse_day, rupees, to_decimal
from sra.upstox.client import UpstoxError, shared_client
from sra.upstox.market import full_quotes

NOT_FOUND = 'Name one company, e.g. "TCS dividend history".'
TOP_SHOWN = 15

TOP_PAYERS_SQL = """
SELECT ca.instrument_key, i.ticker, i.name, SUM(ca.amount) AS trailing
FROM corporate_actions ca
JOIN instruments i ON i.instrument_key = ca.instrument_key
WHERE ca.kind = 'dividend' AND ca.amount > 0 AND ca.ex_date > %(since)s
  AND i.exchange = 'NSE_EQ'
GROUP BY ca.instrument_key, i.ticker, i.name
"""

NO_STORED_ACTIONS = (
    "Dividend history is not stored yet. Run `sra migrate`, then "
    "`sra upstox ingest-fundamentals --all`, to fill it in."
)


def top_payers(today: date) -> PathResult:
    """Highest trailing-twelve-month yields among stored dividend payers."""
    try:
        with connect(readonly=True) as conn:
            rows = conn.execute(
                TOP_PAYERS_SQL, {"since": today - timedelta(days=365)}
            ).fetchall()
    except psycopg.errors.UndefinedTable:
        return PathResult(text=NO_STORED_ACTIONS)
    if not rows:
        return PathResult(text=NO_STORED_ACTIONS)
    payers = {str(r["instrument_key"]): r for r in rows}
    try:
        quotes = full_quotes(list(payers))
    except UpstoxError as exc:
        return PathResult(text=f"Live prices are unavailable right now ({exc}).")
    yields = []
    for key, row in payers.items():
        if key in quotes:
            price = Decimal(str(quotes[key].last_price))
            trailing = Decimal(str(row["trailing"]))
            yields.append((100 * trailing / price, row, trailing, price))
    yields.sort(key=lambda item: item[0], reverse=True)
    lines = [
        f"  {i}. {row['ticker']} ({row['name']}): {pct:.2f}% "
        f"({rupees(trailing)} a share over 12 months, price {rupees(price)})"
        for i, (pct, row, trailing, price) in enumerate(yields[:TOP_SHOWN], start=1)
    ]
    return PathResult(
        text="\n\n".join(
            [
                f"Highest dividend yields\n{len(yields)} stored companies paid a "
                "dividend in the last 12 months; yield is those dividends over "
                "today's price.",
                "Trailing 12-month yield\n" + "\n".join(lines),
                "Notes\n"
                "  - A one-off special dividend inflates a yield that may not repeat.\n"
                "  - A high yield can also mean the price has fallen; check why.",
            ]
        )
    )


def _events(isin: str) -> list[tuple[date, str, Decimal | None, str]]:
    """(ex-date, kind, amount per share, type) for each event, newest first."""
    payload = shared_client().get_json(
        f"/v2/fundamentals/{isin}/corporate-actions", params={}
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    events = []
    for event in rows or []:
        if not isinstance(event, dict):
            continue
        details = {
            str(d.get("name")): d.get("value")
            for d in event.get("event_details") or []
            if isinstance(d, dict)
        }
        ex = parse_day(
            str(details.get("Ex dividend date") or event.get("expiry_date") or "")
        )
        if ex is None:
            continue
        events.append(
            (
                ex,
                str(event.get("name") or "Event"),
                to_decimal(event.get("amount")),
                str(details.get("Dividend type") or ""),
            )
        )
    return sorted(events, reverse=True)


def answer(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    found = resolve_companies(question)
    if not found.companies:
        return top_payers(today)
    if len(found.companies) != 1:
        return PathResult(text=NOT_FOUND)
    company = found.companies[0]
    isin = company.instrument_key.split("|", 1)[1]
    title = f"{company.name} ({company.ticker}): dividends"
    try:
        events = _events(isin)
        quote = full_quotes([company.instrument_key]).get(company.instrument_key)
    except UpstoxError as exc:
        return PathResult(text=f"{title}\nUnavailable right now ({exc}).")
    dividends = [e for e in events if e[1].lower() == "dividend" and e[2] is not None]
    if not events:
        return PathResult(
            text=f"{title}\nUpstox lists no corporate actions for the last year."
        )

    lines = []
    for ex, kind, amount, kind_detail in events:
        label = f"{kind_detail} {kind.lower()}".strip().capitalize()
        value = f"{rupees(amount)} a share" if amount is not None else "no amount given"
        lines.append(f"  {ex:%d %b %Y}: {label}, {value}")
    sections = [
        f"{title}\nCorporate actions Upstox lists for about the last year.",
        "Events (ex-date)\n" + "\n".join(lines),
    ]
    year = [
        amount for ex, _, amount, _ in dividends if ex > today - timedelta(days=365)
    ]
    if year and quote is not None:
        total = sum((a for a in year if a is not None), Decimal(0))
        price = Decimal(str(quote.last_price))
        sections.append(
            "Trailing 12 months\n"
            f"  Dividends: {rupees(total)} a share\n"
            f"  Yield at today's price {rupees(price)}: {100 * total / price:.2f}%"
        )
    sections.append(
        "A past dividend is not a promise of the next one. Only about a year of "
        "history is available here."
    )
    return PathResult(text="\n\n".join(sections))
