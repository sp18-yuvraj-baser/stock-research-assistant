from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from sra import bharatstock
from sra.agent import derivatives, funds
from sra.agent.companies import Resolution
from sra.agent.derivatives import Strike, max_pain, parse_chain
from sra.ingest.funds_run import parse_nav_file
from sra.upstox.instruments import EquityInstrument

NAV_FILE = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

PPFAS Mutual Fund

122639;INF879O01027;-;Parag Parikh Flexi Cap Fund;Direct Plan;Growth;88.9623;29-Oct-2026
153964;-;INF879O01308;Parag Parikh Flexi Cap Fund;Direct Plan;Monthly IDCW Payout;N.A.;29-Oct-2026
"""  # noqa: E501


def test_nav_file_carries_category_and_fund_house() -> None:
    rows = parse_nav_file(NAV_FILE, datetime(2026, 10, 30, tzinfo=UTC))
    first, second = rows
    assert first["category"] == "Equity Scheme - Flexi Cap Fund"
    assert first["fund_house"] == "PPFAS Mutual Fund"
    assert first["isin"] == "INF879O01027" and str(first["nav"]) == "88.9623"
    assert first["nav_date"] == date(2026, 10, 29)
    assert second["isin"] == "INF879O01308" and second["nav"] is None


def test_unexpected_file_is_refused() -> None:
    from sra.feeds import FeedError

    with pytest.raises(FeedError):
        parse_nav_file("<html>maintenance</html>", datetime.now(UTC))


def test_fund_returns_use_nav_on_or_before_each_anniversary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 10, 29)
    history = tuple(
        (today - timedelta(days=d), 100.0 * (1.1 ** ((2000 - d) / 365)))
        for d in range(2000, -1, -1)
    )
    monkeypatch.setattr(funds, "_history", lambda code, day: history)
    returns = dict(funds.fund_returns("1", today))
    assert returns["1 year"] == pytest.approx(10.0, abs=0.05)
    assert returns["5 years"] == pytest.approx(10.0, abs=0.05)


def _strike(price: float, call: float, put: float) -> Strike:
    return Strike(price, call, put, None, None)


def test_max_pain_minimises_what_buyers_collect() -> None:
    strikes = [_strike(100, 10, 50), _strike(110, 30, 30), _strike(120, 60, 5)]
    # At 110: calls 100*10 = 100; puts (120-110)*5 = 50 -> 150, the least.
    assert max_pain(strikes) == 110


def test_parse_chain_reads_open_interest_and_iv() -> None:
    rows = [
        {
            "strike_price": 22950,
            "underlying_spot_price": 22961.35,
            "call_options": {"market_data": {"oi": 100}, "option_greeks": {"iv": 13.2}},
            "put_options": {"market_data": {"oi": 150}, "option_greeks": {"iv": 15.9}},
        }
    ]
    spot, strikes = parse_chain(rows)
    assert spot == 22961.35
    assert strikes == [Strike(22950, 100, 150, 13.2, 15.9)]


def test_shareholding_needs_the_key_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tcs = EquityInstrument("TCS", "NSE_EQ|TCS", "NSE_EQ", "TATA CONSULTANCY")
    monkeypatch.setattr(
        derivatives, "resolve_companies", lambda q: Resolution([tcs], [])
    )
    monkeypatch.setattr(bharatstock, "configured", lambda: False)
    assert (
        "SRA_BHARATSTOCK_API_KEY" in derivatives.shareholding("TCS shareholding").text
    )


def test_shareholding_lists_quarters(monkeypatch: pytest.MonkeyPatch) -> None:
    tcs = EquityInstrument("TCS", "NSE_EQ|TCS", "NSE_EQ", "TATA CONSULTANCY")
    monkeypatch.setattr(
        derivatives, "resolve_companies", lambda q: Resolution([tcs], [])
    )

    def fake_get(path: str, params: dict[str, str], *, max_age: Any) -> Any:
        assert path == "/stocks/TCS/shareholding"
        quarter = {"as_on_date": "2026-09-30", "promoter_pct": 71.77}
        quarter |= {"fii_pct": 11.5, "dii_pct": 10.9, "public_pct": 5.8}
        return {"data": [quarter]}

    monkeypatch.setattr(bharatstock, "get", fake_get)
    text = derivatives.shareholding("TCS shareholding").text
    assert "Promoters: 2026-09-30 71.77%" in text
    assert "mutual funds" not in text  # absent field, not shown as n/a


def test_ipos_and_bulk_deals_are_declined() -> None:
    assert "IPO details" in derivatives.not_covered("upcoming IPOs").text
    assert "bulk and block deals" in derivatives.not_covered("bulk deals today").text
