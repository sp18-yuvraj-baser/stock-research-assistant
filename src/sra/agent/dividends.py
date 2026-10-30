"""Answer dividend questions from Upstox's corporate actions, live.

Upstox returns about the last year of events, which is exactly what a
trailing-twelve-month yield needs: the sum of dividends that went ex in the
last 365 days, divided by today's price.
"""

from datetime import date, timedelta
from decimal import Decimal

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.figures import parse_day, rupees, to_decimal
from sra.upstox.client import UpstoxError, shared_client
from sra.upstox.market import full_quotes

NOT_FOUND = 'Name one company, e.g. "TCS dividend history".'


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
