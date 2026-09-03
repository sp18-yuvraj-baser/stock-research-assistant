from datetime import date

INSTANT = "instant"
QUARTER = "quarter"
YTD = "ytd"
ANNUAL = "annual"
OTHER = "other"

# A filer's own period lengths drift: 52/53-week fiscal calendars make a
# "quarter" anywhere from 89 to 98 days. These bands are loose at the edges and
# leave gaps between them, so an unusual period lands in OTHER rather than being
# mislabelled.
_DURATION_BANDS: tuple[tuple[int, int, str], ...] = (
    (80, 100, QUARTER),
    (165, 200, YTD),
    (255, 290, YTD),
    (350, 380, ANNUAL),
)


def duration_days(period_start: date | None, period_end: date) -> int | None:
    """Inclusive-of-both-endpoints length, matching how filers state periods.

    A 10-Q covering 2026-04-27 through 2026-07-26 is a 91-day quarter, not 90.
    """
    if period_start is None:
        return None
    return (period_end - period_start).days + 1


def classify_period(period_start: date | None, period_end: date) -> str:
    """Distinguish the figures one filing tags at the same period_end.

    A 10-Q reports both the discrete quarter and the cumulative year-to-date
    total, both ending on the quarter end and both carrying the same fiscal
    label. Without this, "revenue last quarter" can return the half-year total.
    """
    days = duration_days(period_start, period_end)
    if days is None:
        return INSTANT
    for low, high, kind in _DURATION_BANDS:
        if low <= days <= high:
            return kind
    return OTHER
