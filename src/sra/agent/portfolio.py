"""Review a whole portfolio pasted into the chat, rendered in code.

"100 TCS at 3500, 50 INFY at 1400, HDFCBANK 200" becomes one holding per
part, priced with a single batched live quote. Weights, gains and sector
concentration are computed here; like the single-holding review it never
says what to buy or sell.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from sra.agent import holding
from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.agent.universe import load
from sra.figures import rupees
from sra.upstox.client import UpstoxError
from sra.upstox.instruments import EquityInstrument
from sra.upstox.market import full_quotes

DISCLAIMER = (
    "This is research, not investment advice. For a personal recommendation, "
    "consult a SEBI-registered investment adviser."
)

# One stock above this share of the portfolio is called out.
CONCENTRATION_PCT = Decimal(25)

# Commas inside numbers ("1,000") are not separators.
_PARTS = r"[;\n]|,(?!\d{2,3}\b)|\band\b|\bplus\b"
_NUMBER = r"\d[\d,]{0,15}(?:\.\d{1,4})?"
_PRICE = re.compile(
    r"(?:@|\bat\b|\bavg\.?|\baverage(?: price| cost)?|\bcost(?: price)?|\bbought at)"
    rf"\s*(?:rs\.?|₹|inr)?\s*({_NUMBER})",
    re.IGNORECASE,
)
_QUANTITY = re.compile(
    r"(?<![\d.])(\d[\d,]{0,15})(?![\d.])(?!\s*(?:lakhs?|lacs?|crores?|cr\b|k\b|%|rs\b"
    r"|rupees|inr|₹))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Holding:
    company: EquityInstrument
    quantity: Decimal
    buy_price: Decimal | None


def _decimal(text: str) -> Decimal | None:
    value = Decimal(text.replace(",", ""))
    return value if value > 0 else None


def parse_holdings(question: str) -> list[Holding]:
    """Holdings named one per part; a part without exactly one company and a
    share count is skipped."""
    holdings: list[Holding] = []
    for part in re.split(_PARTS, question, flags=re.IGNORECASE):
        if not part or not part.strip():
            continue
        found = resolve_companies(part)
        if len(found.companies) != 1:
            continue
        price_match = _PRICE.search(part)
        buy_price = _decimal(price_match.group(1)) if price_match else None
        rest = (
            part[: price_match.start()] + part[price_match.end() :]
            if price_match
            else part
        )
        quantity_match = _QUANTITY.search(rest)
        quantity = _decimal(quantity_match.group(1)) if quantity_match else None
        company = found.companies[0]
        if quantity is not None and all(h.company != company for h in holdings):
            holdings.append(Holding(company, quantity, buy_price))
    return holdings


def review_holdings(holdings: list[Holding]) -> PathResult:
    keys = [h.company.instrument_key for h in holdings]
    try:
        quotes = full_quotes(keys)
    except UpstoxError as exc:
        return PathResult(text=f"Live prices are unavailable right now ({exc}).")
    sectors = {k: f.sector for k, f in load(keys).companies.items()}

    priced = [h for h in holdings if h.company.instrument_key in quotes]
    value = {
        h.company.ticker: h.quantity
        * Decimal(str(quotes[h.company.instrument_key].last_price))
        for h in priced
    }
    total = sum(value.values(), Decimal(0))
    if not priced or total <= 0:
        return PathResult(text="No live prices came back for these holdings.")

    rows = []
    for h in holdings:
        ticker = h.company.ticker
        if ticker not in value:
            rows.append(f"  {ticker}: no live price, left out of the totals")
            continue
        price = Decimal(str(quotes[h.company.instrument_key].last_price))
        line = (
            f"  {ticker}: {h.quantity.normalize():f} shares at {rupees(price)} = "
            f"{rupees(value[ticker])} ({100 * value[ticker] / total:.1f}%)"
        )
        if h.buy_price is not None:
            gain = value[ticker] - h.quantity * h.buy_price
            line += (
                f", {'gain' if gain >= 0 else 'loss'} {rupees(abs(gain))} "
                f"({100 * gain / (h.quantity * h.buy_price):+.2f}%)"
            )
        rows.append(line)

    costed = [h for h in priced if h.buy_price is not None]
    totals = [f"  Value: {rupees(total)}"]
    if costed:
        cost = sum((h.quantity * (h.buy_price or 0) for h in costed), Decimal(0))
        gain = sum((value[h.company.ticker] for h in costed), Decimal(0)) - cost
        totals.append(f"  Cost of holdings with a buy price: {rupees(cost)}")
        totals.append(
            f"  Unrealised {'gain' if gain >= 0 else 'loss'} on them: "
            f"{rupees(abs(gain))} ({100 * gain / cost:+.2f}%)"
        )

    concentration = []
    largest = max(value, key=lambda t: value[t])
    share = 100 * value[largest] / total
    concentration.append(
        f"  Largest holding: {largest} at {share:.1f}% of the portfolio"
        + (
            f" (over {CONCENTRATION_PCT}% in one stock)"
            if share > CONCENTRATION_PCT
            else ""
        )
    )
    by_sector: dict[str, Decimal] = {}
    for h in priced:
        sector = (
            sectors.get(h.company.instrument_key)
            or "Sector not stored yet (refresh fundamentals)"
        )
        by_sector[sector] = by_sector.get(sector, Decimal(0)) + value[h.company.ticker]
    concentration += [
        f"  {sector}: {100 * amount / total:.1f}%"
        for sector, amount in sorted(by_sector.items(), key=lambda kv: -kv[1])
    ]

    sections = [
        f"Portfolio review: {len(holdings)} holdings\n"
        "I can't tell you to buy, sell or hold, but here is what the data says.",
        "Holdings at live prices\n" + "\n".join(rows),
        "Totals\n" + "\n".join(totals),
        "Concentration\n" + "\n".join(concentration),
        "Before deciding, ask yourself\n"
        "  - Is any one stock or sector a bigger share than you could stomach "
        "falling by half?\n"
        '  - Ask "Should I keep my <SYMBOL> shares?" for a full review of any one '
        "holding, or about tax before selling.",
        DISCLAIMER,
    ]
    return PathResult(text="\n\n".join(sections))


NO_HOLDINGS = (
    "List your holdings one per line or separated by commas, e.g. "
    '"100 TCS at 3500, 50 INFY at 1400, 200 HDFCBANK". A buy price is optional.'
)


def answer(question: str) -> PathResult:
    """Every holding question: two or more holdings get the portfolio review,
    anything else the single-holding review (which also handles no share
    count, an ambiguous name or no company at all)."""
    holdings = parse_holdings(question)
    if len(holdings) > 1:
        return review_holdings(holdings)
    asks_portfolio = re.search(r"\bportfolio\b", question, re.IGNORECASE)
    if not holdings and asks_portfolio and not resolve_companies(question).companies:
        return PathResult(text=NO_HOLDINGS)
    return holding.review(question)
