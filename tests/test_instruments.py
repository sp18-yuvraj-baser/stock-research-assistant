import pytest

from sra.upstox.instruments import UnknownInstrumentError, resolve_instruments

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
    assert resolved["TCS"] == ("NSE_EQ|INE467B01029", "NSE_EQ")
    assert resolved["INFY"] == ("NSE_EQ|INE009A01021", "NSE_EQ")


def test_unknown_ticker_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sra.upstox.instruments.fetch_instrument_master",
        lambda **_kwargs: _FIXTURE_RECORDS,
    )
    with pytest.raises(UnknownInstrumentError, match="RELIANCE"):
        resolve_instruments(["TCS", "RELIANCE"])
