from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from sra.agent import watchlist
from sra.upstox.instruments import EquityInstrument
from sra.upstox.market import FullQuote

TCS = EquityInstrument("TCS", "NSE_EQ|TCS", "NSE_EQ", "TATA CONSULTANCY")
INFY = EquityInstrument("INFY", "NSE_EQ|INFY", "NSE_EQ", "INFOSYS")


class _Db:
    """An in-memory watchlist behind the two queries the module runs."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def execute(self, sql: str, params: Any = None) -> "_Db":
        if sql.startswith("INSERT"):
            self.rows.setdefault(params[0], params[1])
        elif sql.startswith("DELETE"):
            self.rows.pop(params[0], None)
        return self

    def fetchall(self) -> list[dict[str, str]]:
        return [{"instrument_key": k, "ticker": t} for k, t in self.rows.items()]

    def commit(self) -> None:
        pass


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    store = _Db()

    @contextmanager
    def fake_connect(*, readonly: bool = False) -> Iterator[_Db]:
        yield store

    by_word = {"tcs": TCS, "infosys": INFY}
    monkeypatch.setattr(watchlist, "connect", fake_connect)
    monkeypatch.setattr(
        watchlist,
        "companies_in",
        lambda q: [c for w, c in by_word.items() if w in q.lower()],
    )
    monkeypatch.setattr(
        watchlist,
        "full_quotes",
        lambda keys: {
            k: FullQuote(k, 100.0, 1.0, None, None, None, None, None, None, None)
            for k in keys
        },
    )
    monkeypatch.setattr(watchlist, "daily_candles", lambda key, **_: [])
    return store


def test_add_show_and_remove(db: _Db) -> None:
    assert watchlist.answer("show my watchlist").text == watchlist.EMPTY
    added = watchlist.answer("add TCS and Infosys to my watchlist").text
    assert "Added: TCS, INFY" in added
    assert "TCS: ₹100.00 (+1.01% today)" in added
    removed = watchlist.answer("remove TCS from my watchlist").text
    assert "Removed: TCS" in removed and "  TCS:" not in removed
    assert list(db.rows) == ["NSE_EQ|INFY"]


def test_adding_twice_keeps_one_entry(db: _Db) -> None:
    watchlist.answer("add TCS to my watchlist")
    watchlist.answer("add TCS to my watchlist")
    assert list(db.rows) == ["NSE_EQ|TCS"]


def test_no_stock_named_asks_for_one(db: _Db) -> None:
    assert "Name the stocks" in watchlist.answer("add to my watchlist").text


def test_a_rewritten_follow_up_cannot_decide_to_write(db: _Db) -> None:
    watchlist.answer("add TCS to my watchlist")
    # The rewrite says "remove", but the user only asked to see the list.
    text = watchlist.answer(
        "remove TCS from my watchlist", typed="and what does my watchlist look like?"
    ).text
    assert "Removed" not in text and list(db.rows) == ["NSE_EQ|TCS"]


def test_the_rewrite_may_still_name_the_stock(db: _Db) -> None:
    watchlist.answer("add TCS to my watchlist", typed="add it to my watchlist")
    assert list(db.rows) == ["NSE_EQ|TCS"]
