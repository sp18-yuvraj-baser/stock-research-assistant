from datetime import date, timedelta

import pytest

from sra.agent import history
from sra.agent.history import chart_points, price_stats, requested_years
from sra.upstox.market import Candle, FullQuote

TODAY = date(2026, 10, 30)


def _candles(days: int, start_price: float, end_price: float) -> list[Candle]:
    first = TODAY - timedelta(days=days)
    step = (end_price - start_price) / days
    return [
        Candle(first + timedelta(days=d), p, p, p, p, 1)
        for d in range(days)
        for p in [start_price + d * step]
    ]


def test_price_stats_reports_range_and_returns() -> None:
    candles = _candles(6 * 365, 100.0, 220.0)
    stats = price_stats(candles, 220.0, TODAY)
    assert stats is not None
    assert stats.high.price == 220.0 and stats.high.day == TODAY
    assert stats.low.day == TODAY - timedelta(days=364)
    returns = {label: (total, per_year) for label, total, per_year in stats.returns}
    total, per_year = returns["5 years"]
    assert total is not None and per_year is not None
    assert per_year == pytest.approx(100 * ((1 + total / 100) ** (1 / 5) - 1))
    assert returns["1 year"][1] is None  # no per-year figure under 3 years


def test_short_history_says_so_rather_than_guessing() -> None:
    stats = price_stats(_candles(200, 100.0, 120.0), 120.0, TODAY)
    assert stats is not None
    returns = {label: total for label, total, _ in stats.returns}
    assert returns["1 year"] is None and returns["6 months"] is not None


@pytest.mark.parametrize(
    ("question", "years"),
    [
        ("chart of TCS", 1.0),
        ("TCS 5 year return", 5.0),
        ("infosys six months performance", 0.5),
        ("reliance 30 year chart", 10.0),
    ],
)
def test_requested_span(question: str, years: float) -> None:
    assert requested_years(question) == years


def test_chart_is_thinned_but_keeps_the_last_close() -> None:
    candles = _candles(2000, 100.0, 300.0)
    points = chart_points(candles, candles[0].day)
    assert len(points) == history.MAX_CHART_POINTS
    assert points[-1] == (candles[-1].day.isoformat(), candles[-1].close)


def test_answer_draws_a_chart_and_computes_from_the_live_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "NSE_INDEX|Nifty 50"
    monkeypatch.setattr(
        history, "daily_candles", lambda k, **_: _candles(6 * 365, 100.0, 200.0)
    )
    live = FullQuote(key, 210.0, 10.0, None, None, None, None, None, None, None)
    monkeypatch.setattr(history, "full_quotes", lambda keys: {key: live})
    result = history.answer("Nifty 50 1 year return", today=TODAY)
    assert result.text.startswith(
        "Nifty 50: price history\nPrice ₹210.00, +5.00% today"
    )
    assert "High: ₹210.00 on 30 Oct 2026" in result.text
    assert result.chart is not None and result.chart.title == "Nifty 50, 1 year"


def test_no_target_asks_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from sra.agent.companies import Resolution

    monkeypatch.setattr(history, "resolve_companies", lambda q: Resolution([], []))
    assert history.answer("show me a chart").text == history.NOT_FOUND
