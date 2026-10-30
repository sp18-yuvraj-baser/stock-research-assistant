from datetime import date
from decimal import Decimal

import pytest

from sra.agent import screen
from sra.agent.screen import DISCLAIMER, Pick
from sra.agent.universe import Ratio, Series

TODAY = date(2026, 10, 29)


def _years(*values: float, last: int = 2026) -> Series:
    """Yearly March figures ending in `last`, oldest first."""
    first = last - len(values) + 1
    return [(date(first + i, 3, 1), Decimal(str(v))) for i, v in enumerate(values)]


def _company(**overrides: object) -> tuple[dict[str, Ratio], dict[str, Series]]:
    ratios: dict[str, Ratio] = {
        "ROE": (Decimal(25), Decimal(15)),
        "P/E": (Decimal(20), Decimal(25)),
    }
    series = {
        "revenue": _years(8000, 9000, 10000, 11000),
        "net_profit": _years(800, 900, 1000, 1100),
        "total_asset": _years(10000, 10000, 10000, 10000),
        "total_liability": _years(4000, 4000, 4000, 4000),
    }
    for key, value in overrides.items():
        if key in ratios or key in ("NIM", "Net NPA"):
            ratios[key] = value  # type: ignore[assignment]
        else:
            series[key] = value  # type: ignore[assignment]
    return ratios, series


def test_a_steady_profitable_company_passes() -> None:
    passed = screen._passes(*_company(), TODAY)
    assert passed is not None
    roe, pe, pe_sector, revenue_growth, profit_growth, latest = passed
    assert (roe, pe, pe_sector) == (Decimal(25), Decimal(20), Decimal(25))
    assert revenue_growth == Decimal("11.2") and profit_growth == Decimal("11.2")
    assert latest == date(2026, 3, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"revenue": _years(4000, 4500, 4800, 4900)},  # too small
        {"net_profit": _years(800, 900, 850, 1100)},  # a down year
        {"net_profit": _years(800, 900, 1000, 2000)},  # a merger-sized jump
        {"revenue": _years(8000, 9000, 10000, 11000, last=2023)},  # stale
        {"ROE": (Decimal(12), Decimal(10))},  # ROE under 15%
        {"ROE": (Decimal(18), Decimal(22))},  # ROE below its sector
        {"total_liability": _years(7000, 7000, 7000, 7000)},  # 70% leverage
        {"P/E": (Decimal(-5), Decimal(25))},  # loss-making
        {"P/E": (Decimal(40), Decimal(25))},  # over 1.5x sector
    ],
)
def test_each_test_can_fail_a_company(overrides: dict[str, object]) -> None:
    assert screen._passes(*_company(**overrides), TODAY) is None


def test_banks_use_npa_instead_of_leverage() -> None:
    bank = _company(
        ROE=(Decimal(13), Decimal(10)),
        NIM=(Decimal(3), Decimal(3)),
        **{"Net NPA": (Decimal("0.4"), Decimal("0.8"))},
        total_liability=_years(9000, 9000, 9000, 9000),
    )
    assert screen._passes(*bank, TODAY) is not None
    risky = _company(
        ROE=(Decimal(13), Decimal(10)),
        NIM=(Decimal(3), Decimal(3)),
        **{"Net NPA": (Decimal(2), Decimal("0.8"))},
    )
    assert screen._passes(*risky, TODAY) is None


def test_an_unusable_sector_pe_falls_back_to_a_fixed_cap() -> None:
    assert screen._passes(*_company(**{"P/E": (Decimal(45), Decimal("0.3"))}), TODAY)
    assert not screen._passes(*_company(**{"P/E": (Decimal(70), None)}), TODAY)


def _pick(ticker: str, roe: int, growth: str, pe: int) -> Pick:
    return Pick(
        ticker, ticker, Decimal(roe), Decimal(pe), Decimal(20),
        Decimal(10), Decimal(growth), "Mar 2026",
    )  # fmt: skip


def test_ranking_balances_quality_growth_and_price() -> None:
    picks = [
        _pick("A", 40, "5", 30),
        _pick("B", 20, "30", 10),
        _pick("C", 25, "15", 20),
    ]
    assert [p.ticker for p in screen._rank(picks)] == ["B", "C", "A"]


def test_answer_states_tests_and_never_promises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picks = [_pick("INFY", 33, "6.9", 15)]
    monkeypatch.setattr(screen, "screen_stocks", lambda today: (picks, None))
    text = screen.run("suggest some stocks").text
    assert "Tests every company passed" in text
    assert "1. INFY (INFY): ROE 33%" in text
    assert "can't tell you what to buy or promise profits" in text
    assert text.endswith(DISCLAIMER)


def test_no_passing_company_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen, "screen_stocks", lambda today: ([], None))
    text = screen.run("suggest some stocks").text
    assert "No company passes every test" in text
