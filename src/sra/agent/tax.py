"""Estimate capital-gains tax on selling listed Indian shares, in code.

Rates are those in force for sales from 23 July 2024 (Finance (No. 2) Act,
2024), stated with the date in every answer: short-term (held 12 months or
less) at 20%, long-term at 12.5% on gains above ₹1.25 lakh a financial year.
Shares bought before 1 February 2018 use the grandfathered cost: the higher
of the actual cost and the lower of the 31 January 2018 high and the sale
price. Cess and surcharge are left out and said so.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

from sra.agent.loop import PathResult
from sra.agent.portfolio import parse_holdings
from sra.figures import rupees
from sra.upstox.client import UpstoxError
from sra.upstox.market import full_quotes

RATES_AS_OF = "rates for sales from 23 July 2024, as of FY2026-27"
STCG_RATE = Decimal(20)
LTCG_RATE = Decimal("12.5")
LTCG_EXEMPTION = Decimal(125_000)
GRANDFATHER_CUTOFF = date(2018, 2, 1)

_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_NUMBER = r"(\d[\d,]{0,15}(?:\.\d{1,4})?)"

RULES = (
    "How it works\n"
    f"  - Held 12 months or less: short-term gain, taxed at {STCG_RATE}%.\n"
    f"  - Held over 12 months: long-term gain, taxed at {LTCG_RATE}% on the part "
    f"of the year's long-term gains above {rupees(LTCG_EXEMPTION)}.\n"
    "  - Bought before 1 Feb 2018: cost is the higher of what you paid and the "
    "lower of the 31 Jan 2018 high and the sale price.\n"
    "  - A short-term loss can be set off against short- or long-term gains; a "
    "long-term loss only against long-term gains; unused losses carry forward "
    "8 years if you file your return on time."
)

NOTES = (
    "Notes\n"
    f"  - {RATES_AS_OF[0].upper() + RATES_AS_OF[1:]}; 4% cess and any surcharge are "
    "not included.\n"
    "  - Assumes STT was paid on purchase and sale (shares bought and sold on "
    "the exchange).\n"
    f"  - The {rupees(LTCG_EXEMPTION)} exemption is shared by all your long-term "
    "gains in the financial year.\n"
    "  - This is an estimate, not tax advice; a chartered accountant can confirm."
)


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, candidate)
        except ValueError:
            continue
    raise ValueError(day)


def parse_buy_date(question: str, today: date) -> tuple[date | None, str]:
    """The purchase date a question gives, and how it was read."""
    text = question.lower()
    if m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? {_MONTH},? (\d{{4}})\b", text):
        day = date(int(m.group(3)), _MONTHS.index(m.group(2)) + 1, int(m.group(1)))
        return day, f"{day:%d %b %Y}"
    if m := re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text):
        day = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return day, f"{day:%d %b %Y} (read as day/month/year)"
    if m := re.search(rf"\b{_MONTH},? (\d{{4}})\b", text):
        day = date(int(m.group(2)), _MONTHS.index(m.group(1)) + 1, 1)
        return day, f"{day:%b %Y} (taken as the 1st)"
    if m := re.search(r"\b(\d+|a|one|two|three) (years?|months?) ago\b", text):
        amount = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(
            m.group(1)
        )
        months = amount * (12 if m.group(2).startswith("year") else 1)
        day = _add_months(today, -months)
        return day, f"about {day:%b %Y} ({m.group(0)})"
    if m := re.search(r"\b(?:in|during) (20\d\d|19\d\d)\b", text):
        day = date(int(m.group(1)), 1, 1)
        return (
            day,
            f"{m.group(1)} (taken as 1 Jan; give the month for a precise answer)",
        )
    return None, ""


def _sell_price(question: str) -> Decimal | None:
    # Not across "bought": in "sell 100 TCS bought at 1000", 1000 is the cost.
    match = re.search(
        r"\b(?:sell|selling|sold)\b(?:(?!\b(?:bought|purchased|paid)\b)[^.?!]){0,25}?"
        rf"\b(?:at|for)\b\s*(?:rs\.?|₹|inr)?\s*{_NUMBER}",
        question,
        re.IGNORECASE,
    )
    return Decimal(match.group(1).replace(",", "")) if match else None


def _tax_lines(gain: Decimal, long_term: bool) -> list[str]:
    if gain <= 0:
        return [f"  No tax: this is a {'long' if long_term else 'short'}-term loss of "
                f"{rupees(-gain)} (see set-off rules below)."]  # fmt: skip
    if long_term:
        taxable = max(Decimal(0), gain - LTCG_EXEMPTION)
        tax = (taxable * LTCG_RATE / 100).quantize(Decimal("0.01"))
        return [
            f"  Long-term gain {rupees(gain)} minus exemption {rupees(LTCG_EXEMPTION)} "
            f"= taxable {rupees(taxable)}",
            f"  Tax at {LTCG_RATE}%: {rupees(tax)}",
        ]
    tax = (gain * STCG_RATE / 100).quantize(Decimal("0.01"))
    return [f"  Short-term gain {rupees(gain)} at {STCG_RATE}% = {rupees(tax)}"]


def answer(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    # The holding the question describes: "sell 100 TCS bought at 1000" or
    # "100 shares of TCS at 1000"; the portfolio parser reads both forms.
    holdings = parse_holdings(question)
    named = holdings[0] if len(holdings) == 1 else None
    if named is None or named.buy_price is None:
        return PathResult(
            text="Capital-gains tax on listed shares\n"
            "For an estimate, give the company, share count, buy price and buy date, "
            'e.g. "tax if I sell 100 TCS bought at 3500 in March 2023".\n\n'
            f"{RULES}\n\n{NOTES}"
        )
    company, quantity, buy_price = named.company, named.quantity, named.buy_price
    sell = _sell_price(question)
    sell_source = "the price you gave"
    try:
        if sell is None:
            quote = full_quotes([company.instrument_key]).get(company.instrument_key)
            if quote is not None:
                sell, sell_source = Decimal(str(quote.last_price)), "today's live price"
    except UpstoxError:
        pass
    if sell is None:
        return PathResult(text='No sale price: give one ("sell at 2500") or try again.')

    bought, bought_note = parse_buy_date(question, today)
    title = f"{company.ticker}: tax estimate on selling"
    inputs = [
        f"  Shares: {quantity.normalize():f}",
        f"  Buy price: {rupees(buy_price)}"
        + (f", bought {bought_note}" if bought else ""),
        f"  Sale price: {rupees(sell)} ({sell_source})",
    ]
    grandfathered = bought is not None and bought < GRANDFATHER_CUTOFF
    if grandfathered:
        # Upstox's price history is adjusted for later bonuses and splits, so
        # its 31 Jan 2018 price is not the fair value the law uses; a wrong
        # one would understate the cost. The actual cost is used and the user
        # is pointed at the official figure.
        inputs.append(
            "  Bought before 1 Feb 2018: actual cost used. Your cost may be "
            "higher (the 31 Jan 2018 fair value, capped at the sale price); "
            "the exchange's published value for that day settles it."
        )
    gain = (quantity * (sell - buy_price)).quantize(Decimal("0.01"))

    sections = [
        f"{title}\nIndian capital-gains tax, {RATES_AS_OF}.",
        "Inputs\n" + "\n".join(inputs),
    ]
    if bought is None:
        sections.append(
            "If held over 12 months (long-term)\n" + "\n".join(_tax_lines(gain, True))
        )
        sections.append(
            "If held 12 months or less (short-term)\n"
            + "\n".join(_tax_lines(gain, False))
        )
    else:
        long_term = today > _add_months(bought, 12)
        held = (
            "over 12 months, so long-term"
            if long_term
            else "12 months or less, so short-term"
        )
        sections.append(f"Tax (held {held})\n" + "\n".join(_tax_lines(gain, long_term)))
        if not long_term:
            turns = _add_months(bought, 12) + timedelta(days=1)
            sections.append(
                f"Timing\n  Sold on or after {turns:%d %b %Y}, the same gain would be "
                f"long-term ({LTCG_RATE}% above the exemption) instead of {STCG_RATE}%."
            )
    sections += [RULES, NOTES]
    return PathResult(text="\n\n".join(sections))
