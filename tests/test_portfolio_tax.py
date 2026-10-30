from datetime import date
from decimal import Decimal

import pytest

from sra.agent import holding, portfolio, tax
from sra.agent.companies import Resolution
from sra.agent.loop import PathResult
from sra.agent.universe import CompanyFacts, Universe
from sra.upstox.instruments import EquityInstrument
from sra.upstox.market import FullQuote

TCS = EquityInstrument("TCS", "NSE_EQ|TCS", "NSE_EQ", "TATA CONSULTANCY")
INFY = EquityInstrument("INFY", "NSE_EQ|INFY", "NSE_EQ", "INFOSYS")
HDFC = EquityInstrument("HDFCBANK", "NSE_EQ|HDFC", "NSE_EQ", "HDFC BANK")
TODAY = date(2026, 10, 30)


def _quote(key: str, price: float) -> FullQuote:
    return FullQuote(key, price, None, None, None, None, None, None, None, None)


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch) -> None:
    by_word = {"tcs": TCS, "infy": INFY, "infosys": INFY, "hdfcbank": HDFC}

    def fake_resolve(text: str) -> Resolution:
        words = text.lower().replace(",", " ").split()
        return Resolution([c for w, c in by_word.items() if w in words], [])

    prices = {
        TCS.instrument_key: 2500.0,
        INFY.instrument_key: 1500.0,
        HDFC.instrument_key: 700.0,
    }
    monkeypatch.setattr(portfolio, "resolve_companies", fake_resolve)
    for module in (portfolio, tax):
        monkeypatch.setattr(
            module, "full_quotes", lambda keys: {k: _quote(k, prices[k]) for k in keys}
        )
    sectors = {
        TCS.instrument_key: "IT",
        INFY.instrument_key: "IT",
        HDFC.instrument_key: "Banks",
    }
    monkeypatch.setattr(
        portfolio,
        "load",
        lambda keys: Universe(
            {k: CompanyFacts(k, k, k, sector=sectors[k]) for k in keys}, None
        ),
    )


def test_holdings_are_parsed_one_per_part(market: None) -> None:
    holdings = portfolio.parse_holdings(
        "100 TCS at 3,500, 50 INFY @ 1400 and HDFCBANK 1,000"
    )
    assert [(h.company.ticker, h.quantity, h.buy_price) for h in holdings] == [
        ("TCS", Decimal(100), Decimal(3500)),
        ("INFY", Decimal(50), Decimal(1400)),
        ("HDFCBANK", Decimal(1000), None),
    ]


def test_portfolio_review_weights_gains_and_sectors(market: None) -> None:
    text = portfolio.answer(
        "my portfolio: 100 TCS at 2000, 100 INFY at 1500, 500 HDFCBANK"
    ).text
    assert text.startswith("Portfolio review: 3 holdings")
    assert (
        "TCS: 100 shares at ₹2,500.00 = ₹250,000.00 (33.3%), gain ₹50,000.00 (+25.00%)"
        in text
    )
    assert "Value: ₹750,000.00" in text
    assert "Unrealised gain on them: ₹50,000.00 (+14.29%)" in text
    assert (
        "Largest holding: HDFCBANK at 46.7% of the portfolio (over 25% in one stock)"
        in text
    )
    assert "  IT: 53.3%" in text and "  Banks: 46.7%" in text


def test_one_holding_goes_to_the_holding_review(
    market: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(holding, "review", lambda q: PathResult(text="single"))
    assert portfolio.answer("I have 100 TCS shares, should I sell?").text == "single"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("bought on 15 March 2022", date(2022, 3, 15)),
        ("bought 3rd jan, 2021", date(2021, 1, 3)),
        ("bought 05/11/2023", date(2023, 11, 5)),
        ("bought in March 2023", date(2023, 3, 1)),
        ("bought 2 years ago", date(2024, 10, 30)),
        ("bought in 2017", date(2017, 1, 1)),
        ("no date here", None),
    ],
)
def test_buy_dates(question: str, expected: date | None) -> None:
    assert tax.parse_buy_date(question, TODAY)[0] == expected


def test_long_term_gain_uses_the_exemption(market: None) -> None:
    text = tax.answer(
        "tax if I sell 100 TCS bought at 1000 in March 2023", today=TODAY
    ).text
    assert "Sale price: ₹2,500.00 (today's live price)" in text
    assert (
        "Long-term gain ₹150,000.00 minus exemption ₹125,000.00 = taxable ₹25,000.00"
        in text
    )
    assert "Tax at 12.5%: ₹3,125.00" in text
    assert "cess" in text


def test_short_term_gain_and_when_it_turns_long_term(market: None) -> None:
    text = tax.answer(
        "tax if I sell 10 TCS bought at 2000 on 1 June 2026 and sell at 2600",
        today=TODAY,
    ).text
    assert "Sale price: ₹2,600.00 (the price you gave)" in text
    assert "Short-term gain ₹6,000.00 at 20% = ₹1,200.00" in text
    assert "Sold on or after 02 Jun 2027" in text


def test_pre_2018_purchase_uses_actual_cost_and_says_why(market: None) -> None:
    text = tax.answer("tax on selling 10 TCS bought at 1000 in 2016", today=TODAY).text
    assert "Bought before 1 Feb 2018: actual cost used." in text
    assert "Long-term gain ₹15,000.00" in text


def test_no_figures_explains_the_rules(market: None) -> None:
    text = tax.answer("what is LTCG on shares?").text
    assert text.startswith("Capital-gains tax on listed shares")
    assert "Held over 12 months" in text
