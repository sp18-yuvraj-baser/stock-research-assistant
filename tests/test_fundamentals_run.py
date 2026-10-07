import pytest

from sra.ingest.fundamentals_run import (
    _balance_sheet_rows,
    _income_statement_rows,
    _parse_ratio_value,
    _ratio_rows,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8.94%", 8.94),
        ("20.15", 20.15),
        (None, None),
        ("n/a", None),
    ],
)
def test_parse_ratio_value(raw: str | None, expected: float | None) -> None:
    assert _parse_ratio_value(raw) == expected


def test_ratio_rows_parses_percent_and_plain_values() -> None:
    payload = {
        "data": [
            {"name": "P/E", "company_value": "20.15", "sector_value": "12.46"},
            {"name": "ROE", "company_value": "8.94%", "sector_value": "16.46%"},
        ]
    }
    rows = _ratio_rows(payload)
    assert rows[0] == {"name": "P/E", "company_value": 20.15, "sector_value": 12.46}
    assert rows[1] == {"name": "ROE", "company_value": 8.94, "sector_value": 16.46}


def test_income_statement_rows_flattens_category_history() -> None:
    payload = {
        "data": {
            "income_statement": [
                {
                    "category": "revenue",
                    "history": [
                        {"value": 1000, "period": "FY2025", "change": "10%"},
                        {"value": 900, "period": "FY2024", "change": "5%"},
                    ],
                }
            ]
        }
    }
    rows = _income_statement_rows(payload, time_period="yearly")
    assert rows == [
        {
            "statement": "income_statement",
            "time_period": "yearly",
            "category": "revenue",
            "period": "FY2025",
            "value": 1000,
        },
        {
            "statement": "income_statement",
            "time_period": "yearly",
            "category": "revenue",
            "period": "FY2024",
            "value": 900,
        },
    ]


def test_balance_sheet_rows_pivots_flat_history_into_categories() -> None:
    payload = {
        "data": {
            "history": [
                {"period": "FY2025", "total_asset": 5000, "total_liability": 2000},
            ]
        }
    }
    rows = _balance_sheet_rows(payload, time_period="yearly")
    assert rows == [
        {
            "statement": "balance_sheet",
            "time_period": "yearly",
            "category": "total_asset",
            "period": "FY2025",
            "value": 5000,
        },
        {
            "statement": "balance_sheet",
            "time_period": "yearly",
            "category": "total_liability",
            "period": "FY2025",
            "value": 2000,
        },
    ]
