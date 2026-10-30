from datetime import date
from decimal import Decimal

import pytest

from sra.agent import calculators, explain
from sra.agent.companies import Resolution
from sra.agent.loop import PathResult
from sra.agent.universe import CompanyFacts, Universe
from sra.upstox.instruments import EquityInstrument
from sra.upstox.market import FullQuote


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("SIP of 5000 a month for 10 years at 12%", "Estimated value: ₹1,161,695.38"),
        ("SIP of 5k per month for 6 months at 0%", "Estimated value: ₹30,000.00"),
        ("invest 2 lakh for 5 years at 10%", "Estimated value: ₹322,102.00"),
        ("CAGR from 100 to 250 in 5 years", "CAGR: 20.11% a year"),
        ("position size with capital 5 lakh, risk 1%, entry 500, stop 470",
         "Shares: 166"),
        ("break-even for 100 shares bought at 2500, brokerage 20",
         "Buy-side charges: ₹320.16"),
    ],
)  # fmt: skip
def test_calculators(question: str, expected: str) -> None:
    assert expected in calculators.answer(question).text


def test_break_even_covers_both_sides() -> None:
    text = calculators.answer("break-even for 100 shares bought at 2500").text
    assert "Brokerage: ₹20.00 an order (assumed; give yours)" in text
    sell = Decimal(
        text.split("Break-even sale price: ₹")[1].split(" ")[0].replace(",", "")
    )
    cost = 250_000 + calculators.charges(
        Decimal(250_000), buy=True, brokerage=Decimal(20)
    )
    net = sell * 100 - calculators.charges(sell * 100, buy=False, brokerage=Decimal(20))
    assert net >= cost
    assert (sell - Decimal("0.05")) * 100 - calculators.charges(
        (sell - Decimal("0.05")) * 100, buy=False, brokerage=Decimal(20)
    ) < cost


def test_missing_numbers_get_the_examples() -> None:
    assert calculators.answer("SIP calculator please").text.startswith("Calculators")


def test_stop_above_entry_is_refused() -> None:
    text = calculators.answer(
        "position size capital 1 lakh risk 1% entry 100 stop 110"
    ).text
    assert "stop loss must be below the entry price" in text


TCS = EquityInstrument("TCS", "NSE_EQ|TCS", "NSE_EQ", "TATA CONSULTANCY")


def _facts() -> CompanyFacts:
    year = date(2026, 3, 1)
    return CompanyFacts(
        TCS.instrument_key,
        "TCS",
        TCS.name,
        ratios={"ROE": (Decimal("43.37"), None), "P/E": (Decimal(20), None)},
        yearly={
            "net_profit": [(year, Decimal(50))],
            "total_asset": [(year, Decimal(200))],
            "total_liability": [(year, Decimal(100))],
            "revenue": [(year, Decimal(250))],
        },
    )


@pytest.fixture
def tcs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explain, "resolve_companies", lambda q: Resolution([TCS], []))
    monkeypatch.setattr(
        explain, "load", lambda keys: Universe({TCS.instrument_key: _facts()}, None)
    )
    monkeypatch.setattr(
        explain,
        "full_quotes",
        lambda keys: {
            k: FullQuote(k, 2500.0, None, None, None, None, None, None, None, None)
            for k in keys
        },
    )


def test_roe_is_worked_through_and_set_beside_upstox(tcs: None) -> None:
    text = explain.answer("how is TCS's ROE calculated?").text
    assert "Equity = 200.00 - 100.00 = 100.00" in text
    assert "ROE = 50.00 / 100.00 = 50.00%" in text
    assert "Upstox's ROE: 43.37%" in text


def test_pe_implies_eps_from_the_live_price(tcs: None) -> None:
    text = explain.answer("explain the P/E of TCS").text
    assert "EPS behind it is about ₹2,500.00 / 20 = ₹125.00 a share" in text


def test_without_a_company_the_explainer_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(explain, "resolve_companies", lambda q: Resolution([], []))
    monkeypatch.setattr(
        explain, "general_finance_path", lambda q: PathResult(text="edu")
    )
    assert explain.answer("how is ROE calculated?").text == "edu"
