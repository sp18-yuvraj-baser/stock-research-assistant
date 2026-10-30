from datetime import date

import pytest

from sra.agent import market
from sra.upstox.market import FullQuote, find_index


def _quote(key: str, price: float, change: float, volume: int = 100) -> FullQuote:
    return FullQuote(key, price, change, None, None, None, volume, price, None, None)


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("how is bank nifty today", "Nifty Bank"),
        ("what is nifty at", "Nifty 50"),
        ("sensex?", "Sensex"),
        ("tcs price", None),
    ],
)
def test_find_index_prefers_the_longest_alias(text: str, name: str | None) -> None:
    found = find_index(text)
    assert (found.name if found else None) == name


@pytest.mark.parametrize(
    ("status", "words"),
    [
        ("NORMAL_OPEN", "open"),
        ("PRE_OPEN_START", "in the pre-open session"),
        ("CLOSING_END", "closed"),
        (None, "status unknown"),
    ],
)
def test_status_words(status: str | None, words: str) -> None:
    assert market._status_words(status) == words


def test_movers_rank_by_change_and_value_traded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = {f"K{i}": (f"T{i}", f"Name {i}") for i in range(7)}
    quotes = {
        key: _quote(key, 100.0 + i, float(i - 3), volume=10_000_000 * (7 - i))
        for i, key in enumerate(universe)
    }
    monkeypatch.setattr(market, "_universe", lambda: universe)
    monkeypatch.setattr(market, "full_quotes", lambda keys: quotes)
    text = market._movers()
    gainers = text.split("Gainers\n")[1].split("\n\n")[0]
    losers = text.split("Losers\n")[1].split("\n\n")[0]
    active = text.split("Most active by value traded\n")[1].split("\n\n")[0]
    assert gainers.splitlines()[0].startswith("  T6 ")
    assert losers.splitlines()[0].startswith("  T0 ")
    assert active.splitlines()[0].startswith("  T0 ")
    assert "Among 7 established NSE companies" in text


def test_overview_lists_the_named_index_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market, "full_quotes", lambda keys: {k: _quote(k, 1000.0, 10.0) for k in keys}
    )
    monkeypatch.setattr(market, "market_status", lambda exchange: "NORMAL_OPEN")
    monkeypatch.setattr(market, "upcoming_holidays", lambda today: [])
    text = market.answer("how is bank nifty doing", today=date(2026, 10, 30)).text
    assert text.startswith("Nifty Bank and the wider market")
    indices = text.split("Indices\n")[1].splitlines()
    assert indices[0].startswith("  Nifty Bank: 1,000.00 (+10.00, +1.01%)")
    assert "NSE is open now." in text
