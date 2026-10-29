import time
from decimal import Decimal
from typing import Any

import pytest

from sra.agent import holding
from sra.agent.holding import DISCLAIMER, NO_COMPANY, Position, parse_position
from sra.tools.get_live_quote import LiveQuote
from sra.tools.run_sql import SqlResult

INSTRUMENTS = [
    {"ticker": "HDFCBANK", "name": "HDFC BANK LTD"},
    {"ticker": "INFY", "name": "INFOSYS LIMITED"},
    {"ticker": "TCS", "name": "TATA CONSULTANCY SERV LT"},
    {"ticker": "TATAPOWER", "name": "TATA POWER CO LTD"},
    {"ticker": "TATASTEEL", "name": "TATA STEEL LIMITED"},
    {"ticker": "LT", "name": "LARSEN & TOUBRO LTD."},
    {"ticker": "SBIN", "name": "STATE BANK OF INDIA"},
    {"ticker": "ACE", "name": "ACTION CONSTRUCTION EQUIP"},
    {"ticker": "GLOBAL", "name": "GLOBAL EDUCATION LIMITED"},
]

HDFC_RATIOS = [
    {"name": "P/E", "company_value": "13.01", "sector_value": "0.32"},
    {"name": "P/B", "company_value": "1.8", "sector_value": "1.46"},
    {"name": "ROE", "company_value": "13.75", "sector_value": "8.82"},
    {"name": "Net NPA", "company_value": "0.38", "sector_value": "0.79"},
    {"name": "NIM", "company_value": "3.28", "sector_value": "4.25"},
]

HDFC_FINANCIALS = [
    {"category": "revenue", "period": "Mar 2023", "value": "204666.1"},
    {"category": "revenue", "period": "Mar 2024", "value": "407994.77"},
    {"category": "revenue", "period": "Mar 2025", "value": "470915.93"},
    {"category": "revenue", "period": "Mar 2026", "value": "495462.81"},
    {"category": "net_profit", "period": "Mar 2026", "value": "70000"},
    {"category": "net_profit", "period": "Mar 2025", "value": "73440.17"},
]


class FakeData:
    def __init__(self) -> None:
        self.ratios: list[dict[str, Any]] = HDFC_RATIOS
        self.financials: list[dict[str, Any]] = HDFC_FINANCIALS
        self.price: float | None = 682.5
        self.errors: dict[str, str] = {}
        self.quotes: list[str] = []

    def run_sql(self, sql: str, params: dict[str, Any] | None = None) -> SqlResult:
        assert params is not None
        if "FROM instruments" in sql:
            if "instruments" in self.errors:
                return SqlResult(sql=sql, error=self.errors["instruments"])
            rows = [
                r
                for r in INSTRUMENTS
                if r["ticker"] in params["tickers"]
                or r["name"] in params["names"]
                or any(r["name"].startswith(p[:-1]) for p in params["prefixes"])
            ]
        elif "fundamental_ratios" in sql:
            if "ratios" in self.errors:
                return SqlResult(sql=sql, error=self.errors["ratios"])
            stamp = "2026-10-22T09:36:00+00:00"
            rows = [{**r, "fetched_at": stamp} for r in self.ratios]
        else:
            if "financials" in self.errors:
                return SqlResult(sql=sql, error=self.errors["financials"])
            rows = self.financials
        return SqlResult(sql=sql, rows=rows)

    def get_live_quote(self, ticker: str, *, exchange: str | None = None) -> LiveQuote:
        self.quotes.append(f"{ticker}:{exchange}")
        return LiveQuote(
            ticker, "NSE_EQ|X", "NSE_EQ", self.price, "2026-10-29T07:02:00+00:00"
        )


@pytest.fixture
def data(monkeypatch: pytest.MonkeyPatch) -> FakeData:
    fake = FakeData()
    monkeypatch.setattr(holding, "run_sql", fake.run_sql)
    monkeypatch.setattr(holding, "get_live_quote", fake.get_live_quote)
    return fake


def _section(text: str, heading: str) -> str:
    return text.split(f"{heading}\n", 1)[1].split("\n\n", 1)[0]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("I have 100 hdfc bank stocks, should I keep them?", Position(Decimal(100))),
        (
            "Should I sell TCS? I bought 20 at 3,500.",
            Position(Decimal(20), Decimal(3500)),
        ),
        (
            "I hold 1,000 shares of INFY, avg price 1450.5",
            Position(Decimal(1000), Decimal("1450.5")),
        ),
        ("I have 300 Eternal shares at 250 avg", Position(Decimal(300), Decimal(250))),
        (
            "I have 100 shares of Dmart at 3500 cost",
            Position(Decimal(100), Decimal(3500)),
        ),
        (
            "I have 5 shares of MRF bought at 1,20,000",
            Position(Decimal(5), Decimal(120000)),
        ),
        ("I have 100 shares, trading at 500 now", Position(Decimal(100))),
        ("Can I hold SBIN for 5 years?", Position()),
        ("Is HDFCBANK down 20% a good buy?", Position()),
        # Money and portfolio counts are never a share count.
        (
            "I hold 2.5 lakh rupees of ITC at an average of 410",
            Position(None, Decimal(410)),
        ),
        ("I put 1 lakh in hdfc bank shares", Position()),
        ("I have Rs 50,000 worth of hdfc bank shares", Position()),
        ("holding 30 stocks in my portfolio including Wipro, sell Wipro?", Position()),
        ("I have 10 TCS and 20 INFY shares", Position(conflicting=True)),
        ("I have 50000 invested in TCS, should I sell?", Position()),
        ("I have 50,000 in TCS shares, keep?", Position()),
        (
            "I have 100 shares; I bought 100 shares at 500",
            Position(Decimal(100), Decimal(500)),
        ),
    ],
)
def test_parse_position(question: str, expected: Position) -> None:
    assert parse_position(question) == expected


def test_review_renders_every_section_with_computed_figures(data: FakeData) -> None:
    text = holding.review(
        "I have 100 hdfc bank stocks bought at 600, should I keep or sell them?"
    ).text

    assert text.startswith("HDFC BANK LTD (HDFCBANK): review")
    assert data.quotes == ["HDFCBANK:NSE"]
    assert "Live price: ₹682.50 (NSE, 29 Oct 2026, 12:32 IST)" in text
    assert "Your 100 shares: ₹68,250.00" in text
    assert "unrealised gain ₹8,250.00 (+13.75%)" in text
    assert "P/E 13.01 vs sector 0.32 (sector figure looks unreliable)" in text
    assert "P/B 1.8 vs sector 1.46: pricier than sector" in text
    assert "Net NPA 0.38% vs sector 0.79%: better than sector (lower is better)" in text
    # Only the last three years are shown, oldest first, sorted as dates.
    assert "Revenue: Mar 2024 407,994.77 → Mar 2025 470,915.93 → Mar 2026" in text
    assert "move of over 50% in Mar 2024" in text
    assert "Net profit fell 4.68% in the year to Mar 2026" in _section(
        text, "Points against"
    )
    assert "P/E" not in _section(text, "Points in favour")
    assert text.endswith(DISCLAIMER)


def test_review_never_gives_a_verdict(data: FakeData) -> None:
    text = holding.review("Should I sell my HDFCBANK shares?").text.lower()
    for verdict in ("you should", "we recommend", "i recommend", "sell now"):
        assert verdict not in text


def test_negative_equal_and_missing_values(data: FakeData) -> None:
    data.ratios = [
        {"name": "P/E", "company_value": "-16.68", "sector_value": "23.07"},
        {"name": "ROE", "company_value": "-20", "sector_value": "10"},
        {"name": "Net NPA", "company_value": "0", "sector_value": "0.8"},
        {"name": "P/B", "company_value": "2", "sector_value": "2"},
        {"name": "ROA", "company_value": "NaN", "sector_value": "5"},
        {"name": "ROCE", "company_value": "12", "sector_value": None},
        {"name": "CASA", "company_value": "27.18", "sector_value": "-2.04"},
    ]
    data.financials = [
        {"category": "net_profit", "period": "Mar 2025", "value": "-50"},
        {"category": "net_profit", "period": "Mar 2026", "value": "80"},
        {"category": "revenue", "period": "Mar 2025", "value": None},
        {"category": "revenue", "period": "Mar 2026", "value": "300"},
    ]
    text = holding.review("Should I sell my HDFCBANK shares?").text

    against = _section(text, "Points against")
    favour = _section(text, "Points in favour")
    assert "P/E -16.68: not meaningful" in against
    assert "ROE -20% vs sector 10%: weaker than sector" in against
    assert "Net NPA 0% vs sector 0.8%: better than sector" in favour
    assert "P/B 2 vs sector 2: in line with sector" in text
    assert "P/B" not in favour and "P/B" not in against
    assert "ROA" not in text
    assert "ROCE 12% (no sector figure)" in text
    assert "CASA 27.18% vs sector -2.04% (sector figure looks unreliable)" in text
    assert "Net profit went from a loss to a profit in the year to Mar 2026" in favour


def test_large_drops_are_flagged_not_counted(data: FakeData) -> None:
    data.financials = [
        {"category": "revenue", "period": "Mar 2025", "value": "1000"},
        {"category": "revenue", "period": "Mar 2026", "value": "300"},
    ]
    text = holding.review("Should I sell my HDFCBANK shares?").text
    assert "move of over 50% in Mar 2026" in text
    assert "Revenue" not in _section(text, "Points against")


def test_gap_between_periods_is_named(data: FakeData) -> None:
    data.financials = [
        {"category": "revenue", "period": "Mar 2023", "value": "100"},
        {"category": "revenue", "period": "Mar 2026", "value": "110"},
    ]
    text = holding.review("Should I sell my HDFCBANK shares?").text
    assert "Revenue grew 10.00% from Mar 2023 to Mar 2026" in text


def test_zero_price_is_unavailable(data: FakeData) -> None:
    data.price = 0.0
    text = holding.review("I have 100 HDFCBANK shares bought at 90, sell?").text
    assert "Live price unavailable" in text
    assert "Your 100 shares" not in text


def test_lookup_errors_are_not_reported_as_missing_data(data: FakeData) -> None:
    data.errors = {"ratios": "timeout", "financials": "timeout"}
    text = holding.review("Should I sell my HDFCBANK shares?").text
    assert text.count("Data lookup failed: timeout") == 2
    assert "No ratio data" not in text

    data.errors = {"instruments": "connection refused"}
    text = holding.review("Should I sell my HDFCBANK shares?").text
    assert "couldn't look up the company just now (connection refused)" in text


def test_review_without_a_company_asks_for_one(data: FakeData) -> None:
    result = holding.review("Which stock should I buy?")
    assert result.text == NO_COMPANY
    assert result.quote_calls == []


def test_review_asks_when_the_name_is_ambiguous(data: FakeData) -> None:
    text = holding.review("Should I keep my tata shares?").text
    assert "TATAPOWER (TATA POWER CO LTD)" in text
    assert "TATASTEEL (TATA STEEL LIMITED)" in text
    assert data.quotes == []


def test_two_companies_are_never_merged(data: FakeData) -> None:
    text = holding.review("Should I sell TCS and buy INFY?").text
    assert "You mentioned TCS and INFY" in text
    assert data.quotes == []

    # "infosys shares" names a holding as surely as "TCS" does.
    text = holding.review("I have 10 TCS and 20 infosys shares, which to keep?").text
    assert "You mentioned TCS and INFY" in text

    # A lone lowercase word is only set aside, and neither share count is
    # pinned on the company that is reviewed.
    text = holding.review(
        "I have 10 TCS and 20 shares of another, what about infosys?"
    ).text
    assert text.startswith("TATA CONSULTANCY SERV LT (TCS): review")
    assert "Also matched INFY (INFOSYS LIMITED)" in text
    assert "Position value left out" in text
    assert "shares: ₹" not in text


def test_a_common_word_matching_a_name_is_named_not_reviewed(data: FakeData) -> None:
    text = holding.review("Should I keep HDFC Bank after the RBI action?").text
    assert text.startswith("HDFC BANK LTD (HDFCBANK): review")
    assert "Also matched ACE (ACTION CONSTRUCTION EQUIP)" in text


@pytest.mark.parametrize(
    ("question", "ticker"),
    [
        ("Should I sell my L&T shares?", "LT"),
        ("I own 50 qty of SBI, sell or hold?", "SBIN"),
        ("Should I sell my State Bank of India shares?", "SBIN"),
        ("Is it time to sell my infosys shares?", "INFY"),
    ],
)
def test_company_names_and_aliases_resolve(
    data: FakeData, question: str, ticker: str
) -> None:
    companies = holding.resolve_companies(question).companies
    assert [c.ticker for c in companies] == [ticker]


def test_parsing_a_huge_number_run_stays_fast() -> None:
    started = time.perf_counter()
    parse_position("I have " + "1," * 8000)
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    ("question", "companies", "set_aside"),
    [
        # A lowercase everyday word that is also a ticker loses to the company
        # the user plainly named, and is reported rather than reviewed.
        (
            "Should I sell my Infosys shares given the global slowdown?",
            ["INFY"],
            ["GLOBAL"],
        ),
        # A second company named only ambiguously is reported, not dropped.
        (
            "Should I sell HDFCBANK or my tata shares?",
            ["HDFCBANK"],
            ["TATAPOWER", "TATASTEEL", "TCS"],
        ),
    ],
)
def test_weak_and_ambiguous_matches_are_set_aside(
    data: FakeData, question: str, companies: list[str], set_aside: list[str]
) -> None:
    found = holding.resolve_companies(question)
    assert [c.ticker for c in found.companies] == companies
    assert [c.ticker for c in found.set_aside] == set_aside
    assert found.confident


def test_a_lone_lowercase_word_is_not_confident(data: FakeData) -> None:
    found = holding.resolve_companies("How is the global economy doing?")
    assert [c.ticker for c in found.companies] == ["GLOBAL"]
    assert not found.confident
