from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from sra.ingest import instruments_run
from sra.upstox.instruments import EquityInstrument


class _Conn:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def execute(self, query: str, params: dict[str, Any]) -> None:
        self.calls.append((query, params))

    def commit(self) -> None:
        pass


def test_explicit_ticker_uses_master_name_and_never_clobbers_a_stored_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def fake_connect(*, readonly: bool = False) -> Iterator[_Conn]:
        yield _Conn(calls)

    monkeypatch.setattr(instruments_run, "connect", fake_connect)
    monkeypatch.setattr(
        instruments_run,
        "resolve_instruments",
        lambda tickers, refresh=False: {
            "TCS": ("NSE_EQ|INE467B01029", "NSE_EQ"),
            "ODD": ("NSE_EQ|INE000000000", "NSE_EQ"),
        },
    )
    monkeypatch.setattr(
        instruments_run,
        "list_indian_equities",
        lambda **_k: [
            EquityInstrument("TCS", "NSE_EQ|INE467B01029", "NSE_EQ", "TATA CONSULTANCY")
        ],
    )
    reports = instruments_run.ingest_instruments(["tcs", "odd"])
    assert [r.ticker for r in reports] == ["TCS", "ODD"]
    (query, first), (_, second) = calls
    assert first["name"] == "TATA CONSULTANCY"
    # No master entry: the ticker is only a placeholder, and the SQL must
    # keep a stored real name rather than overwrite it.
    assert second["name"] == "ODD"
    assert "THEN instruments.name" in query


def test_no_tickers_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        instruments_run.ingest_instruments([])


def _eq(ticker: str, key: str, exchange: str = "NSE_EQ") -> EquityInstrument:
    return EquityInstrument(ticker, key, exchange, ticker.title())


def test_split_conflicting_skips_ticker_held_by_another_key() -> None:
    held = {("TMPV", "NSE_EQ"): "NSE_EQ|OLD", ("TCS", "NSE_EQ"): "NSE_EQ|TCS"}
    equities = [
        _eq("TMPV", "NSE_EQ|NEW"),  # same slot, different key: skip
        _eq("TCS", "NSE_EQ|TCS"),  # same slot, same key: update
        _eq("TMPV", "BSE_EQ|X", "BSE_EQ"),  # different exchange: fine
        _eq("INFY", "NSE_EQ|INFY"),  # new
    ]
    keep, skipped = instruments_run.split_conflicting(equities, held)
    assert [e.instrument_key for e in keep] == [
        "NSE_EQ|TCS",
        "BSE_EQ|X",
        "NSE_EQ|INFY",
    ]
    assert skipped == [
        instruments_run.SkippedInstrument("TMPV", "NSE_EQ", "NSE_EQ|NEW")
    ]


def test_split_conflicting_skips_duplicates_within_the_master() -> None:
    keep, skipped = instruments_run.split_conflicting(
        [_eq("ABC", "NSE_EQ|1"), _eq("ABC", "NSE_EQ|2")], {}
    )
    assert [e.instrument_key for e in keep] == ["NSE_EQ|1"]
    assert [s.instrument_key for s in skipped] == ["NSE_EQ|2"]


def test_ingest_all_upserts_only_non_conflicting_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upserted: list[dict[str, Any]] = []

    class _Result:
        def fetchall(self) -> list[dict[str, str]]:
            return [
                {"instrument_key": "NSE_EQ|OLD", "ticker": "TMPV", "exchange": "NSE_EQ"}
            ]

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def executemany(self, _query: str, rows: list[dict[str, Any]]) -> None:
            upserted.extend(rows)

    class _AllConn:
        def execute(self, _query: str) -> _Result:
            return _Result()

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            pass

    @contextmanager
    def fake_connect(*, readonly: bool = False) -> Iterator[_AllConn]:
        yield _AllConn()

    monkeypatch.setattr(instruments_run, "connect", fake_connect)
    monkeypatch.setattr(
        instruments_run,
        "list_indian_equities",
        lambda **_k: [_eq("TMPV", "NSE_EQ|NEW"), _eq("TCS", "NSE_EQ|TCS")],
    )
    result = instruments_run.ingest_all_indian_equities()
    assert result.synced == 1
    assert [r["ticker"] for r in upserted] == ["TCS"]
    assert [(s.ticker, s.instrument_key) for s in result.skipped] == [
        ("TMPV", "NSE_EQ|NEW")
    ]
