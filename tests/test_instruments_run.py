from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from sra.ingest import instruments_run
from sra.upstox.instruments import EquityInstrument


class _FakeDb:
    """One connection's worth of state: the stored rows it reports and the
    upserts it receives."""

    def __init__(self, held: list[dict[str, str]] | None = None) -> None:
        self.held = held or []
        self.query = ""
        self.upserted: list[dict[str, Any]] = []

    def execute(self, _query: str) -> "_FakeDb":
        return self

    def fetchall(self) -> list[dict[str, str]]:
        return self.held

    def cursor(self) -> "_FakeDb":
        return self

    def __enter__(self) -> "_FakeDb":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def executemany(self, query: str, rows: list[dict[str, Any]]) -> None:
        self.query = query
        self.upserted.extend(rows)

    def commit(self) -> None:
        pass


def _use(monkeypatch: pytest.MonkeyPatch, db: _FakeDb) -> None:
    @contextmanager
    def fake_connect(*, readonly: bool = False) -> Iterator[_FakeDb]:
        yield db

    monkeypatch.setattr(instruments_run, "connect", fake_connect)


def test_explicit_ticker_uses_master_name_and_never_clobbers_a_stored_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDb()
    _use(monkeypatch, db)
    monkeypatch.setattr(
        instruments_run,
        "resolve_instruments",
        lambda tickers, refresh=False: {
            "TCS": EquityInstrument(
                "TCS", "NSE_EQ|INE467B01029", "NSE_EQ", "TATA CONSULTANCY"
            ),
            "ODD": EquityInstrument("ODD", "NSE_EQ|INE000000000", "NSE_EQ", "ODD"),
        },
    )
    result = instruments_run.ingest_instruments(["tcs", "odd"])
    assert [e.ticker for e in result.synced] == ["TCS", "ODD"]
    first, second = db.upserted
    assert first["name"] == "TATA CONSULTANCY"
    # No master name: the ticker is only a placeholder, and the SQL must keep
    # a stored real name rather than overwrite it.
    assert second["name"] == "ODD"
    assert "THEN instruments.name" in db.query


def test_explicit_ticker_held_by_another_key_is_skipped_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDb(
        [{"instrument_key": "NSE_EQ|OLD", "ticker": "TMPV", "exchange": "NSE_EQ"}]
    )
    _use(monkeypatch, db)
    monkeypatch.setattr(
        instruments_run,
        "resolve_instruments",
        lambda tickers, refresh=False: {"TMPV": _eq("TMPV", "NSE_EQ|NEW")},
    )
    result = instruments_run.ingest_instruments(["TMPV"])
    assert result.synced == [] and db.upserted == []
    assert [s.instrument_key for s in result.skipped] == ["NSE_EQ|NEW"]


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
    db = _FakeDb(
        [{"instrument_key": "NSE_EQ|OLD", "ticker": "TMPV", "exchange": "NSE_EQ"}]
    )
    _use(monkeypatch, db)
    monkeypatch.setattr(
        instruments_run,
        "list_indian_equities",
        lambda **_k: [_eq("TMPV", "NSE_EQ|NEW"), _eq("TCS", "NSE_EQ|TCS")],
    )
    result = instruments_run.ingest_all_indian_equities()
    assert [e.ticker for e in result.synced] == ["TCS"]
    assert [r["ticker"] for r in db.upserted] == ["TCS"]
    assert [(s.ticker, s.instrument_key) for s in result.skipped] == [
        ("TMPV", "NSE_EQ|NEW")
    ]
