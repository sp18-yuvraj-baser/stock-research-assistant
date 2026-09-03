from datetime import date

import pytest

from sra.periods import (
    ANNUAL,
    INSTANT,
    OTHER,
    QUARTER,
    YTD,
    classify_period,
    duration_days,
)


def test_duration_is_inclusive_of_both_endpoints() -> None:
    # Nvidia's fiscal Q2 FY2027 as filed: 91 days, not 90.
    assert duration_days(date(2026, 4, 27), date(2026, 7, 26)) == 91


def test_instant_has_no_duration() -> None:
    assert duration_days(None, date(2026, 8, 21)) is None
    assert classify_period(None, date(2026, 8, 21)) == INSTANT


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # A 10-Q tags the discrete quarter and the year-to-date total with the
        # same period_end; only the duration separates them.
        (date(2026, 4, 27), date(2026, 7, 26), QUARTER),
        (date(2026, 1, 26), date(2026, 7, 26), YTD),
        (date(2025, 1, 27), date(2025, 10, 26), YTD),
        (date(2025, 1, 27), date(2026, 1, 25), ANNUAL),
        # 53-week fiscal years run long; still annual.
        (date(2024, 9, 29), date(2025, 9, 27), ANNUAL),
        (date(2024, 2, 1), date(2025, 1, 31), ANNUAL),
    ],
)
def test_classifies_filed_periods(start: date, end: date, expected: str) -> None:
    assert classify_period(start, end) == expected


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2025, 1, 1), date(2025, 2, 15)),  # 46 days
        (date(2025, 1, 1), date(2026, 6, 30)),  # 18 months
        (date(2025, 1, 1), date(2025, 5, 20)),  # between quarter and half-year
    ],
)
def test_unusual_periods_fall_through_to_other(start: date, end: date) -> None:
    # Mislabelling an odd period is worse than declining to label it.
    assert classify_period(start, end) == OTHER
