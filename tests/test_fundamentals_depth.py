from datetime import date
from decimal import Decimal

import pytest

from sra.agent import compare, dividends, results
from sra.agent.companies import Resolution
from sra.agent.loop import PathResult
from sra.agent.universe import CompanyFacts, Universe
from sra.upstox.instruments import EquityInstrument
from sra.upstox.market import FullQuote

TCS = EquityInstrument("TCS", "NSE_EQ|INE467B01029", "NSE_EQ", "TATA CONSULTANCY")
INFY = EquityInstrument("INFY", "NSE_EQ|INE009A01021", "NSE_EQ", "INFOSYS")


def _years(*values: float) -> list[tuple[date, Decimal]]:
    first = 2026 - len(values) + 1
    return [(date(first + i, 3, 1), Decimal(str(v))) for i, v in enumerate(values)]


def _facts(
    listing: EquityInstrument, **yearly: list[tuple[date, Decimal]]
) -> CompanyFacts:
    return CompanyFacts(
        listing.instrument_key,
        listing.ticker,
        listing.name,
        ratios={
            "P/E": (Decimal("17.63"), Decimal("64.59")),
            "ROE": (Decimal("43.37"), Decimal("8.87")),
            "P/B": (Decimal("13"), Decimal("0.3")),  # sector figure is junk
        },
        yearly=yearly,
    )


@pytest.fixture
def two_companies(monkeypatch: pytest.MonkeyPatch) -> None:
    by_word = {"tcs": TCS, "infosys": INFY}

    def fake_resolve(text: str) -> Resolution:
        hits = [c for word, c in by_word.items() if word in text.lower()]
        return Resolution(hits, [])

    monkeypatch.setattr(compare, "resolve_companies", fake_resolve)
    facts = {
        TCS.instrument_key: _facts(
            TCS,
            revenue=_years(200, 220, 240, 266),
            net_profit=_years(40, 44, 48, 50),
            total_asset=_years(100, 100, 100, 100),
            total_liability=_years(41, 41, 41, 41),
        ),
        INFY.instrument_key: _facts(
            INFY,
            revenue=_years(100, 210, 220, 230),  # merger-sized jump
            net_profit=_years(20, 21, 22, 23),
        ),
    }
    monkeypatch.setattr(compare, "load", lambda keys: Universe(facts, None))
    monkeypatch.setattr(
        compare,
        "full_quotes",
        lambda keys: {
            k: FullQuote(k, 1000.0, None, None, None, None, None, None, None, None)
            for k in keys
        },
    )
    monkeypatch.setattr(compare, "daily_candles", lambda key, **_: [])


def test_comparison_lines_companies_up_without_a_winner(two_companies: None) -> None:
    text = compare.answer("Compare TCS vs Infosys").text
    assert text.startswith("Comparison: TCS vs INFY")
    assert "P/E: TCS 17.63 (sector 64.59) · INFY 17.63 (sector 64.59)" in text
    assert "P/B: TCS 13 (sector figure unreliable)" in text
    assert "Revenue: TCS 10.0% · INFY 32.0% (includes a one-year jump over 50%)" in text
    assert "Liabilities / assets: TCS 41% · INFY n/a" in text
    assert "better" not in text.lower() and "winner" not in text.lower()
    assert "SEBI-registered" in text


def test_one_company_falls_back_to_the_fundamentals_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        compare,
        "resolve_companies",
        lambda text: Resolution([TCS] if "TCS" in text else [], []),
    )
    monkeypatch.setattr(
        compare, "fundamentals_path", lambda q: PathResult(text="model")
    )
    monkeypatch.setattr(
        compare, "general_finance_path", lambda q: PathResult(text="edu")
    )
    assert compare.answer("compare TCS").text == "model"
    assert compare.answer("compare stocks and mutual funds").text == "edu"


def _quarters(*values: float) -> list[tuple[date, Decimal]]:
    # Jun 2025 .. Sep 2026 every three months
    days = [date(2025, 6, 1), date(2025, 9, 1), date(2025, 12, 1), date(2026, 3, 1),
            date(2026, 6, 1), date(2026, 9, 1)]  # fmt: skip
    return list(zip(days[-len(values) :], map(Decimal, map(str, values)), strict=True))


def test_results_compare_with_last_quarter_and_last_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(results, "resolve_companies", lambda q: Resolution([TCS], []))
    facts = CompanyFacts(
        TCS.instrument_key,
        "TCS",
        TCS.name,
        quarterly={
            "revenue": _quarters(100, 104, 106, 108, 110, 112),
            "net_profit": _quarters(20, 21, 21, 22, 22, 24),
        },
    )
    monkeypatch.setattr(
        results,
        "load",
        lambda keys, quarterly: Universe({TCS.instrument_key: facts}, None),
    )
    text = results.answer("TCS latest results").text
    assert text.startswith("TATA CONSULTANCY (TCS): quarter to Sep 2026")
    assert "QoQ is against Jun 2026; YoY against Sep 2025" in text
    assert "Revenue: ₹112.00 cr (QoQ +1.82%, YoY +7.69%)" in text
    assert "Net profit: ₹24.00 cr (QoQ +9.09%, YoY +14.29%)" in text
    assert "Net margin: 21.4%" in text


def test_dividend_yield_counts_only_the_last_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dividends, "resolve_companies", lambda q: Resolution([TCS], []))
    events = [
        (date(2026, 10, 14), "Dividend", Decimal(12), "Interim"),
        (date(2026, 1, 10), "Dividend", Decimal(10), "Final"),
        (date(2025, 9, 1), "Dividend", Decimal(99), "Final"),  # over a year ago
    ]
    monkeypatch.setattr(dividends, "_events", lambda isin: events)
    monkeypatch.setattr(
        dividends,
        "full_quotes",
        lambda keys: {
            k: FullQuote(k, 2200.0, None, None, None, None, None, None, None, None)
            for k in keys
        },
    )
    text = dividends.answer("TCS dividends", today=date(2026, 10, 30)).text
    assert "14 Oct 2026: Interim dividend, ₹12.00 a share" in text
    assert "Dividends: ₹22.00 a share" in text
    assert "Yield at today's price ₹2,200.00: 1.00%" in text
