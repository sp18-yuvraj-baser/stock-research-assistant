from datetime import date
from decimal import Decimal

import pytest

from sra.agent import discover
from sra.agent.companies import Resolution
from sra.agent.discover import parse_conditions
from sra.agent.universe import CompanyFacts, Universe
from sra.upstox.instruments import EquityInstrument


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("stocks with ROE above 20% and P/E below 25",
         [("roe", Decimal(20), None), ("pe", None, Decimal(25))]),
        ("revenue growth between 15 and 25",
         [("revenue_growth", Decimal(15), Decimal(25))]),
        ("debt-free companies", [("liabilities_to_assets", None, Decimal(30))]),
        ("sales over 10,000 crore", [("revenue", Decimal(10000), None)]),
        ("good companies", []),
    ],
)  # fmt: skip
def test_parse_conditions(
    question: str, expected: list[tuple[str, Decimal | None, Decimal | None]]
) -> None:
    assert [(c.key, c.low, c.high) for c in parse_conditions(question)] == expected


def _company(
    ticker: str, roe: float, pe: float, revenue: float, sector: str
) -> CompanyFacts:
    years = [(date(2023 + i, 3, 1), Decimal(str(revenue))) for i in range(4)]
    return CompanyFacts(
        f"K|{ticker}",
        ticker,
        f"{ticker} LTD",
        ratios={"ROE": (Decimal(str(roe)), None), "P/E": (Decimal(str(pe)), None)},
        yearly={"revenue": years},
        sector=sector,
    )


@pytest.fixture
def universe(monkeypatch: pytest.MonkeyPatch) -> None:
    companies = [
        _company("AAA", 30, 15, 5000, "IT - Software"),
        _company("BBB", 25, -4, 6000, "IT - Software"),  # loss-making P/E
        _company("CCC", 40, 18, 200, "Banks"),  # below the size floor
        _company("DDD", 22, 30, 9000, "Banks"),
    ]
    monkeypatch.setattr(
        discover, "load", lambda: Universe({c.key: c for c in companies}, None)
    )


def test_screen_skips_negative_multiples_and_tiny_companies(universe: None) -> None:
    text = discover.screen("stocks with ROE above 20% and P/E below 20").text
    assert "1 companies match" in text
    assert "1. AAA (AAA LTD): ROE 30%, P/E 15" in text
    assert "BBB" not in text and "CCC" not in text
    assert "at least ₹500 crore" in text


def test_a_revenue_condition_replaces_the_size_floor(universe: None) -> None:
    text = discover.screen("companies with revenue below 1000 crore").text
    assert "CCC" in text and "at least ₹500 crore" not in text


def test_sector_list_ranks_by_revenue(universe: None) -> None:
    text = discover.sector_list("bank stocks").text
    rows = text.split("Companies\n")[1].splitlines()
    assert rows[0].startswith("  1. DDD") and rows[1].startswith("  2. CCC")


def test_peers_share_the_sector(
    universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = EquityInstrument("AAA", "K|AAA", "NSE_EQ", "AAA LTD")
    monkeypatch.setattr(
        discover, "resolve_companies", lambda q: Resolution([target], [])
    )
    text = discover.peers("peers of AAA").text
    assert text.startswith(
        "AAA and its peers\nSector: IT - Software; 2 stored companies"
    )
    assert "(the company asked about)" in text and "DDD" not in text


def test_unparseable_request_lists_the_filters(universe: None) -> None:
    assert discover.screen("find me something nice").text.startswith("Custom screen\n")
