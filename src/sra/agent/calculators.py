"""Investment calculators, computed in code from the numbers in the question.

SIP and lump-sum growth, CAGR, position size from a risk budget, and the
break-even sale price after Indian delivery-trade charges. Every input the
answer uses is restated, so a misread number is visible at once.
"""

import re
from decimal import Decimal

from sra.agent.loop import PathResult
from sra.figures import rupees

# Delivery (CNC) equity charges on NSE, as of FY2026-27. Brokers differ, so
# brokerage is an input; the statutory parts are fixed by regulation.
STT_PCT = Decimal("0.1")  # on buy and sell value, delivery
EXCHANGE_PCT = Decimal("0.00297")  # NSE transaction charge, each side
SEBI_FEE_PCT = Decimal("0.0001")  # ₹10 a crore, each side
STAMP_DUTY_PCT = Decimal("0.015")  # buy side only
GST_PCT = Decimal(18)  # on brokerage, exchange and SEBI charges
CHARGES_AS_OF = "NSE delivery charges as of FY2026-27"

_MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000,
                "lac": 100_000, "lacs": 100_000, "crore": 10_000_000,
                "crores": 10_000_000, "cr": 10_000_000}  # fmt: skip
_AMOUNT = (
    r"(?:rs\.?|₹|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|crores?|cr)?\b"
)


def _amount(match: re.Match[str], group: int = 1) -> Decimal:
    value = Decimal(match.group(group).replace(",", ""))
    unit = (match.group(group + 1) or "").lower()
    return value * _MULTIPLIERS.get(unit, 1)


def _find(pattern: str, text: str) -> re.Match[str] | None:
    return re.search(pattern, text, re.IGNORECASE)


def _years(text: str) -> Decimal | None:
    match = _find(r"(\d+(?:\.\d+)?)\s*(years?|yrs?|y)\b", text)
    if match:
        return Decimal(match.group(1))
    match = _find(r"(\d+)\s*months?\b", text)
    return Decimal(match.group(1)) / 12 if match else None


def _rate(text: str) -> Decimal | None:
    match = _find(r"(\d+(?:\.\d+)?)\s*(%|percent)", text)
    return Decimal(match.group(1)) if match else None


def sip(text: str) -> str | None:
    monthly = _find(
        rf"{_AMOUNT}\s*(?:a|per|every|/)?\s*(?:month|monthly|pm|p\.m\.)", text
    )
    monthly = monthly or _find(rf"\bsip of\s*{_AMOUNT}", text)
    years, rate = _years(text), _rate(text)
    if not monthly or years is None or rate is None:
        return None
    payment = _amount(monthly)
    months = int(years * 12)
    i = rate / 100 / 12
    value = payment * (((1 + i) ** months - 1) / i) * (1 + i) if i else payment * months
    invested = payment * months
    return (
        "SIP calculator\nMonthly investment at the start of each month, "
        "compounded monthly.\n\n"
        f"Inputs\n  Monthly amount: {rupees(payment)}\n  Period: {years:g} years "
        f"({months} months)\n  Expected return: {rate:g}% a year\n\n"
        f"Result\n  Invested: {rupees(invested)}\n  Estimated value: {rupees(value)}"
        f"\n  Estimated gain: {rupees(value - invested)}\n\n"
        "The return is your assumption, not a forecast; real returns vary year "
        "to year and can be negative."
    )


def lump_sum(text: str) -> str | None:
    principal = _find(rf"\b(?:invest|lump ?sum of|deposit)\s*{_AMOUNT}", text)
    years, rate = _years(text), _rate(text)
    if not principal or years is None or rate is None:
        return None
    amount = _amount(principal)
    value = (
        amount * (1 + rate / 100) ** int(years)
        if years == int(years)
        else (amount * Decimal(str((1 + float(rate) / 100) ** float(years))))
    )
    return (
        "Lump-sum calculator\nCompounded yearly.\n\n"
        f"Inputs\n  Amount: {rupees(amount)}\n  Period: {years:g} years\n"
        f"  Expected return: {rate:g}% a year\n\n"
        f"Result\n  Estimated value: {rupees(value)}\n  Estimated gain: "
        f"{rupees(value - amount)}\n\nThe return is your assumption, not a forecast."
    )


def cagr_calc(text: str) -> str | None:
    span = _find(rf"from\s*{_AMOUNT}\s*to\s*{_AMOUNT}", text)
    years = _years(text)
    if not span or years is None or years <= 0:
        return None
    start, end = _amount(span, 1), _amount(span, 3)
    if start <= 0 or end <= 0:
        return None
    rate = (float(end) / float(start)) ** (1 / float(years)) - 1
    return (
        "CAGR calculator\nThe steady yearly rate that turns the start value into "
        "the end value.\n\n"
        f"Inputs\n  Start: {start:,f}\n  End: {end:,f}\n  Period: {years:g} years\n\n"
        f"Result\n  CAGR: {rate * 100:.2f}% a year\n  Total change: "
        f"{(float(end) / float(start) - 1) * 100:+.2f}%"
    )


def position_size(text: str) -> str | None:
    capital = _find(
        rf"\b(?:capital|account|portfolio)\s*(?:of|is|:)?\s*{_AMOUNT}", text
    )
    risk = _find(r"\brisk(?:ing)?\s*(?:of)?\s*(\d+(?:\.\d+)?)\s*%", text)
    entry = _find(rf"\b(?:entry|buy)\s*(?:at|price|of|:)?\s*{_AMOUNT}", text)
    stop = _find(rf"\bstop(?:\s*loss)?\s*(?:at|of|:)?\s*{_AMOUNT}", text)
    if not (capital and risk and entry and stop):
        return None
    budget = _amount(capital) * Decimal(risk.group(1)) / 100
    entry_price, stop_price = _amount(entry), _amount(stop)
    per_share = entry_price - stop_price
    if per_share <= 0:
        return "Position size\nThe stop loss must be below the entry price for a buy."
    shares = int(budget / per_share)
    return (
        "Position size\nShares to buy so that hitting the stop loses only the "
        "risk budget.\n\n"
        f"Inputs\n  Capital: {rupees(_amount(capital))}\n  Risk per trade: "
        f"{risk.group(1)}% = {rupees(budget)}\n  Entry: {rupees(entry_price)}\n"
        f"  Stop loss: {rupees(stop_price)} (risk {rupees(per_share)} a share)\n\n"
        f"Result\n  Shares: {shares}\n  Position value: {rupees(shares * entry_price)}"
        f"\n  Loss if stopped out: {rupees(shares * per_share)} (before charges)\n\n"
        "A stop can be skipped on a gap down, so the real loss can be larger."
    )


def charges(value: Decimal, *, buy: bool, brokerage: Decimal) -> Decimal:
    """Statutory and broker charges on one side of a delivery trade."""
    exchange = value * EXCHANGE_PCT / 100
    sebi = value * SEBI_FEE_PCT / 100
    stt = value * STT_PCT / 100
    stamp = value * STAMP_DUTY_PCT / 100 if buy else Decimal(0)
    gst = (brokerage + exchange + sebi) * GST_PCT / 100
    return brokerage + exchange + sebi + stt + stamp + gst


def break_even(text: str) -> str | None:
    price = _find(rf"\b(?:bought|buy|buying|entry)\s*(?:at|@|price)?\s*{_AMOUNT}", text)
    quantity = _find(r"\b(\d[\d,]*)\s*(?:shares|qty|quantity)\b", text)
    if not price or not quantity:
        return None
    fee = _find(rf"\bbrokerage\s*(?:of|is|:)?\s*{_AMOUNT}", text)
    brokerage = _amount(fee) if fee else Decimal(20)
    buy_price, qty = _amount(price), Decimal(quantity.group(1).replace(",", ""))
    cost = buy_price * qty + charges(buy_price * qty, buy=True, brokerage=brokerage)
    sell = buy_price
    # Charges grow with the sale value, so step the price up until net
    # proceeds cover the cost (a few paise at a time; converges fast).
    while sell * qty - charges(sell * qty, buy=False, brokerage=brokerage) < cost:
        sell += Decimal("0.05")
    buy_side = charges(buy_price * qty, buy=True, brokerage=brokerage)
    return (
        "Break-even after charges\nThe lowest sale price that returns what the "
        f"delivery trade cost, using {CHARGES_AS_OF}.\n\n"
        f"Inputs\n  Bought: {qty:f} shares at {rupees(buy_price)}\n  Brokerage: "
        f"{rupees(brokerage)} an order{'' if fee else ' (assumed; give yours)'}\n\n"
        f"Result\n  Buy-side charges: {rupees(buy_side)}\n  Break-even sale price: "
        f"{rupees(sell)} ({(sell / buy_price - 1) * 100:.2f}% above cost)\n\n"
        "Charges: STT 0.1% each side, NSE transaction charge, SEBI fee, stamp "
        "duty 0.015% on the buy, GST 18% on brokerage and exchange fees. Check "
        "your broker's contract note for the exact figures."
    )


HELP = (
    "Calculators\n"
    "Give the numbers in the question, for example:\n\n"
    "Examples\n"
    '  - "SIP of 5000 a month for 10 years at 12%"\n'
    '  - "invest 2 lakh for 5 years at 10%"\n'
    '  - "CAGR from 100 to 250 in 5 years"\n'
    '  - "position size with capital 5 lakh, risk 1%, entry 500, stop 470"\n'
    '  - "break-even for 100 shares bought at 2500, brokerage 20"'
)


def answer(question: str) -> PathResult:
    text = question.lower()
    order = [
        (r"\bbreak.?even\b|\bcharges\b|\bbrokerage\b", break_even),
        (r"\bposition siz|\bhow many shares\b", position_size),
        (r"\bcagr\b", cagr_calc),
        (r"\bsip\b|\bmonthly\b|\ba month\b|\bper month\b", sip),
        (r"\blump ?sum\b|\binvest\b", lump_sum),
    ]
    for pattern, calculator in order:
        if re.search(pattern, text):
            result = calculator(question)
            return PathResult(text=result if result else HELP)
    return PathResult(text=HELP)
