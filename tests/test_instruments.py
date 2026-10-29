import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sra.upstox.client import UpstoxError
from sra.upstox.instruments import (
    EquityInstrument,
    UnknownInstrumentError,
    fetch_instrument_master,
    list_indian_equities,
    lookup_instrument,
    match_instruments,
    resolve_instruments,
)

_FIXTURE_RECORDS = [
    {
        "trading_symbol": "TCS",
        "instrument_key": "NSE_EQ|INE467B01029",
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "instrument_type": "EQ",
    },
    {
        "trading_symbol": "INFY",
        "instrument_key": "NSE_EQ|INE009A01021",
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "instrument_type": "EQ",
    },
    # A derivative on the same underlying must not be picked up by an
    # equity-only resolution.
    {
        "trading_symbol": "TCS",
        "instrument_key": "NSE_FO|TCS25JANFUT",
        "segment": "NSE_FO",
        "exchange": "NSE",
        "instrument_type": "FUT",
    },
    # The same company listed on BSE must not leak into an NSE-only
    # resolution: same trading_symbol, different segment.
    {
        "trading_symbol": "TCS",
        "instrument_key": "BSE_EQ|INE467B01029",
        "segment": "BSE_EQ",
        "exchange": "BSE",
        "instrument_type": "A",
    },
]


def test_resolve_known_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sra.upstox.instruments.fetch_instrument_master",
        lambda **_kwargs: _FIXTURE_RECORDS,
    )
    resolved = resolve_instruments(["TCS", "INFY"])
    assert (resolved["TCS"].instrument_key, resolved["TCS"].exchange) == (
        "NSE_EQ|INE467B01029",
        "NSE_EQ",
    )
    assert resolved["INFY"].instrument_key == "NSE_EQ|INE009A01021"


def test_unknown_ticker_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sra.upstox.instruments.fetch_instrument_master",
        lambda **_kwargs: _FIXTURE_RECORDS,
    )
    with pytest.raises(UnknownInstrumentError, match="RELIANCE"):
        resolve_instruments(["TCS", "RELIANCE"])


def test_resolve_bse_equity_series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sra.upstox.instruments.fetch_instrument_master",
        lambda **_kwargs: _FIXTURE_RECORDS,
    )
    tcs = resolve_instruments(["TCS"], segment="BSE_EQ")["TCS"]
    assert (tcs.instrument_key, tcs.exchange) == ("BSE_EQ|INE467B01029", "BSE_EQ")


def test_list_indian_equities_keeps_nse_and_bse_cash_equities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sra.upstox.instruments.fetch_instrument_master",
        lambda **_kwargs: _FIXTURE_RECORDS,
    )

    equities = list_indian_equities()

    assert [(item.ticker, item.exchange) for item in equities] == [
        ("TCS", "BSE_EQ"),
        ("INFY", "NSE_EQ"),
        ("TCS", "NSE_EQ"),
    ]


def _gz(records: list[dict[str, Any]]) -> bytes:
    return gzip.compress(json.dumps(records).encode())


def _serve(monkeypatch: pytest.MonkeyPatch, body: bytes, status: int = 200) -> None:
    monkeypatch.setattr(
        "sra.upstox.instruments.httpx.get",
        lambda url, **_k: httpx.Response(
            status, content=body, request=httpx.Request("GET", url)
        ),
    )


def test_download_is_cached_and_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _gz(_FIXTURE_RECORDS))
    records = fetch_instrument_master(cache_dir=tmp_path / "cache")
    assert records == _FIXTURE_RECORDS
    cached = tmp_path / "cache" / "upstox_instruments.json.gz"
    assert json.loads(gzip.decompress(cached.read_bytes())) == _FIXTURE_RECORDS
    assert [p.name for p in cached.parent.iterdir()] == [cached.name]


def test_truncated_download_does_not_replace_a_good_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _gz(_FIXTURE_RECORDS)
    cached = tmp_path / "upstox_instruments.json.gz"
    cached.write_bytes(good)
    _serve(monkeypatch, good[: len(good) // 2])
    with pytest.raises(UpstoxError, match="not valid"):
        fetch_instrument_master(cache_dir=tmp_path, refresh=True)
    assert cached.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir()] == [cached.name]


def test_failed_cache_write_leaves_the_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = tmp_path / "upstox_instruments.json.gz"
    cached.write_bytes(b"previous")
    _serve(monkeypatch, _gz(_FIXTURE_RECORDS))

    def _fail(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("sra.upstox.instruments.os.replace", _fail)
    with pytest.raises(OSError, match="disk full"):
        fetch_instrument_master(cache_dir=tmp_path, refresh=True)
    assert cached.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == [cached.name]


def test_http_failure_is_an_upstox_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, b"", status=503)
    with pytest.raises(UpstoxError, match="could not download"):
        fetch_instrument_master(cache_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fresh_cache_is_reused_without_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "upstox_instruments.json.gz").write_bytes(_gz(_FIXTURE_RECORDS))

    def _no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("must not download")

    monkeypatch.setattr("sra.upstox.instruments.httpx.get", _no_network)
    assert fetch_instrument_master(cache_dir=tmp_path) == _FIXTURE_RECORDS


class _Conn:
    """Records the one query a lookup runs and returns canned rows."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.sql = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def execute(self, sql: str, params: dict[str, Any]) -> "_Conn":
        self.sql, self.params = sql, params
        return self

    def fetchone(self) -> dict[str, str] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, str]]:
        return self.rows


def _use(monkeypatch: pytest.MonkeyPatch, conn: _Conn) -> list[bool]:
    """Route instruments.connect to conn; returns each call's readonly flag."""
    modes: list[bool] = []

    def fake_connect(*, readonly: bool = False) -> _Conn:
        modes.append(readonly)
        return conn

    monkeypatch.setattr("sra.upstox.instruments.connect", fake_connect)
    return modes


_ROW = {
    "ticker": "TCS",
    "instrument_key": "NSE_EQ|INE467B01029",
    "exchange": "NSE_EQ",
    "name": "TATA CONSULTANCY SERV LT",
}


def test_lookup_prefers_nse_and_reads_as_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn([_ROW])
    readonly = _use(monkeypatch, conn)
    assert lookup_instrument("tcs") == EquityInstrument(
        "TCS", "NSE_EQ|INE467B01029", "NSE_EQ", "TATA CONSULTANCY SERV LT"
    )
    assert readonly == [True]
    assert "exchange = 'NSE_EQ' DESC" in conn.sql
    assert conn.params == {"ticker": "tcs", "segment": None}


def test_lookup_with_an_exchange_pins_the_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn([{**_ROW, "exchange": "BSE_EQ"}])
    _use(monkeypatch, conn)
    lookup_instrument("TCS", exchange="bse")
    assert conn.params["segment"] == "BSE_EQ"


def test_lookup_of_an_unsynced_symbol_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _Conn([]))
    with pytest.raises(UnknownInstrumentError, match="FAKE"):
        lookup_instrument("FAKE")


def test_match_returns_one_preferred_listing_per_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn([_ROW])
    _use(monkeypatch, conn)
    found = match_instruments(tickers=["TCS"], names=[], prefixes=["TATA %"])
    assert [f.ticker for f in found] == ["TCS"]
    assert "DISTINCT ON (m.ticker)" in conn.sql
    assert conn.params["prefixes"] == ["TATA %"]
