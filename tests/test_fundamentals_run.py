import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any, ClassVar

import pytest

from sra.figures import period_start
from sra.ingest import fundamentals_run
from sra.ingest.fundamentals_run import (
    _balance_sheet_rows,
    _income_statement_rows,
    _parse_ratio_value,
    _ratio_rows,
    ingest_fundamentals,
)
from sra.upstox.client import UpstoxAuthError, UpstoxError


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
            "period_end": None,
            "value": 1000,
        },
        {
            "statement": "income_statement",
            "time_period": "yearly",
            "category": "revenue",
            "period": "FY2024",
            "period_end": None,
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
            "period_end": None,
            "value": 5000,
        },
        {
            "statement": "balance_sheet",
            "time_period": "yearly",
            "category": "total_liability",
            "period": "FY2025",
            "period_end": None,
            "value": 2000,
        },
    ]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Mar 2026", date(2026, 3, 1)),
        ("Dec 2003", date(2003, 12, 1)),
        ("FY2025", None),
        ("Q2FY26", None),
        ("Foo 2026", None),
        ("mar 2026", None),
        ("", None),
    ],
)
def test_period_end(label: str, expected: date | None) -> None:
    assert period_start(label) == expected


def test_rows_carry_period_end_for_upstox_labels() -> None:
    payload = {"data": {"history": [{"period": "Mar 2026", "total_asset": 1}]}}
    (row,) = _balance_sheet_rows(payload, time_period="yearly")
    assert row["period_end"] == date(2026, 3, 1)


@pytest.mark.parametrize("payload", [{"data": None}, {}, {"data": []}])
def test_null_or_empty_data_yields_no_rows(payload: dict[str, Any]) -> None:
    assert _ratio_rows(payload) == []
    assert _income_statement_rows(payload, time_period="yearly") == []
    assert _balance_sheet_rows(payload, time_period="yearly") == []


@pytest.mark.parametrize("payload", [None, [], "oops", {"data": "oops"}])
def test_wrong_shaped_payload_is_a_value_error(payload: Any) -> None:
    with pytest.raises(ValueError):
        _ratio_rows(payload)
    with pytest.raises(ValueError):
        _income_statement_rows(payload, time_period="yearly")


def test_ratio_entries_without_a_name_are_skipped() -> None:
    payload = {"data": [{"company_value": "1"}, {"name": "P/E", "company_value": "2"}]}
    assert [r["name"] for r in _ratio_rows(payload)] == ["P/E"]


def test_income_statement_tolerates_null_history() -> None:
    payload = {"data": {"income_statement": [{"category": "revenue", "history": None}]}}
    assert _income_statement_rows(payload, time_period="yearly") == []


# --- ingest_fundamentals orchestration -------------------------------------


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    """Answers the lookup queries and records every write."""

    def __init__(self, writes: list[tuple[str, dict[str, Any]]]) -> None:
        self.writes = writes

    def execute(self, query: str, params: Any = None) -> _Result:
        if "DISTINCT ON (ticker)" in query:
            return _Result(
                [{"ticker": t, "instrument_key": f"NSE_EQ|INE{t}"} for t in params[0]]
            )
        if "SELECT DISTINCT ticker" in query:
            return _Result([{"ticker": "AAA"}, {"ticker": "BBB"}, {"ticker": "CCC"}])
        if "fundamental_ratios fr" in query:
            return _Result([{"ticker": "AAA"}])
        self.writes.append((query, params))
        return _Result([])

    def commit(self) -> None:
        pass


class _FakeClient:
    """Maps a ticker's ISIN to the behaviour of every call made for it."""

    behaviour: ClassVar[dict[str, Any]] = {}

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def get_json(self, path: str, *, params: dict[str, str]) -> Any:
        isin = path.split("/")[3]
        outcome = self.behaviour[isin]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(path)
        if outcome == "garbage":
            return json.loads("{not json")
        return outcome


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def fake_connect(*, readonly: bool = False) -> Iterator[_FakeConn]:
        yield _FakeConn(recorded)

    monkeypatch.setattr(fundamentals_run, "connect", fake_connect)
    monkeypatch.setattr(fundamentals_run, "UpstoxClient", _FakeClient)
    return recorded


def _ok(path: str) -> dict[str, Any]:
    if path.endswith("key-ratios"):
        return {"data": [{"name": "P/E", "company_value": "10", "sector_value": "12"}]}
    if path.endswith("income-statement"):
        history = [{"period": "Mar 2026", "value": 5}]
        return {
            "data": {"income_statement": [{"category": "revenue", "history": history}]}
        }
    return {"data": {"history": []}}


_OK = _ok


def test_all_run_aborts_on_expired_token(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    _FakeClient.behaviour = {
        "INEAAA": _OK,
        "INEBBB": UpstoxAuthError("expired"),
        "INECCC": _OK,
    }
    seen: list[str] = []
    with pytest.raises(UpstoxAuthError):
        ingest_fundamentals(
            all_equities=True, on_progress=lambda _i, _n, r: seen.append(r.ticker)
        )
    # CCC was never attempted, and AAA's rows were already written.
    assert seen == ["AAA"]
    financial_writes = [p for q, p in writes if "fundamental_financials" in q]
    assert financial_writes[0]["period_end"] == date(2026, 3, 1)


def test_all_run_records_bad_payloads_and_continues(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    _FakeClient.behaviour = {
        "INEAAA": "garbage",
        "INEBBB": UpstoxError("giving up"),
        "INECCC": {"data": None},
    }
    reports = ingest_fundamentals(all_equities=True)
    assert [r.ticker for r in reports] == ["AAA", "BBB", "CCC"]
    assert reports[0].error is not None and "bad response" in reports[0].error
    assert reports[1].error == "giving up"
    # `"data": null` is an empty company, not a failure.
    assert reports[2].error is None and reports[2].financials == 0


def test_explicit_ticker_still_raises_upstox_errors(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    _FakeClient.behaviour = {"INEAAA": UpstoxError("boom")}
    with pytest.raises(UpstoxError):
        ingest_fundamentals(["AAA"])


def test_resume_skips_recently_stored_listings(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    _FakeClient.behaviour = {"INEBBB": _OK, "INECCC": _OK}
    reports = ingest_fundamentals(all_equities=True, resume=True)
    assert [(r.ticker, r.skipped) for r in reports] == [
        ("AAA", True),
        ("BBB", False),
        ("CCC", False),
    ]


def test_no_symbols_and_no_all_is_a_value_error(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    with pytest.raises(ValueError, match="--all"):
        ingest_fundamentals([])


def test_all_run_stops_after_too_many_failures_in_a_row(
    writes: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fundamentals_run, "MAX_CONSECUTIVE_FAILURES", 2)
    _FakeClient.behaviour = {
        "INEAAA": UpstoxError("down"),
        "INEBBB": UpstoxError("down"),
        "INECCC": _OK,
    }
    seen: list[str] = []
    with pytest.raises(UpstoxError, match="2 listings failed in a row"):
        ingest_fundamentals(
            all_equities=True, on_progress=lambda _i, _n, r: seen.append(r.ticker)
        )
    assert seen == ["AAA", "BBB"]  # CCC never attempted


def test_a_success_resets_the_failure_streak(
    writes: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fundamentals_run, "MAX_CONSECUTIVE_FAILURES", 2)
    _FakeClient.behaviour = {
        "INEAAA": UpstoxError("down"),
        "INEBBB": _OK,
        "INECCC": UpstoxError("down"),
    }
    reports = ingest_fundamentals(all_equities=True)
    assert [r.error for r in reports] == ["down", None, "down"]
