"""Reading Upstox fundamentals values, shared by ingest and answers.

Period labels look like 'Mar 2026'. They are compared as dates, never as
text ('Sep 2025' sorts after 'Mar 2026' alphabetically), and month names are
looked up rather than parsed with strptime, whose %b depends on the process
locale.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
_PERIOD_LABEL = re.compile(r"^([A-Z][a-z]{2}) ([0-9]{4})$")


def period_start(label: str) -> date | None:
    """'Mar 2026' -> 2026-03-01 (first of the month, for ordering); else None.

    Matches migration 008's backfill of fundamental_financials.period_end.
    """
    match = _PERIOD_LABEL.match(label)
    if match is None or match.group(1) not in _MONTHS:
        return None
    return date(int(match.group(2)), _MONTHS.index(match.group(1)) + 1, 1)


def period_label(start: date) -> str:
    """2026-03-01 -> 'Mar 2026', the inverse of period_start."""
    return f"{_MONTHS[start.month - 1]} {start.year}"


def months_between(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def to_decimal(value: object) -> Decimal | None:
    """A finite Decimal, or None for NULL, NaN or anything unparseable."""
    if value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def pct_change(prior: Decimal, current: Decimal) -> Decimal:
    return (100 * (current - prior) / prior).quantize(Decimal("0.01"))


def rupees(value: Decimal | float) -> str:
    """₹ with thousands separators and paise, e.g. '₹68,250.00'."""
    return f"₹{Decimal(str(value)):,.2f}"
