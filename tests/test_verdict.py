from datetime import date, timedelta

import pytest

from sra.agent.narrate import numbers_check
from sra.agent.verdict import Action, Signal, boundaries, decide, trend
from sra.upstox.market import Candle


def _signals(**groups: int) -> list[Signal]:
    return [
        Signal(f"{group} {i}", 1 if points > 0 else -1, group)
        for group, points in groups.items()
        for i in range(abs(points))
    ]


@pytest.mark.parametrize(
    ("groups", "owns", "action"),
    [
        ({"valuation": 1, "quality": 1, "growth": 1}, True, Action.BUY_MORE),
        ({"valuation": 1, "quality": 1, "growth": 1}, False, Action.BUY),
        ({"valuation": 1, "quality": -1, "growth": 1}, True, Action.HOLD),
        ({"valuation": -1, "quality": -1, "growth": 1}, True, Action.HOLD),
        ({"valuation": -1, "quality": -1, "growth": -1}, True, Action.REDUCE),
        ({"valuation": -1, "quality": -1, "growth": -1}, False, Action.AVOID),
        ({"valuation": -2, "quality": -2, "growth": -1}, True, Action.SELL),
    ],
)
def test_score_ladder(groups: dict[str, int], owns: bool, action: Action) -> None:
    assert decide(_signals(**groups), owns=owns).action is action


def test_each_group_is_capped() -> None:
    # Five cheap multiples cannot outweigh weak quality and falling profit.
    verdict = decide(_signals(valuation=5, quality=-2, growth=-1), owns=True)
    assert verdict.score == -1 and verdict.action is Action.HOLD


def test_too_few_signals_stay_on_hold_with_low_conviction() -> None:
    verdict = decide(_signals(valuation=-2), owns=True)
    assert (verdict.action, verdict.conviction) == (Action.HOLD, "low")
    assert decide([], owns=False).action is Action.WAIT


def test_conviction_needs_breadth_and_strength() -> None:
    strong = _signals(valuation=2, quality=2, growth=2, trend=1)
    assert decide(strong, owns=True).conviction == "high"
    narrow = _signals(valuation=2, quality=2)
    assert decide(narrow, owns=True).conviction == "low"


def test_boundaries_name_the_neighbouring_calls() -> None:
    assert boundaries(Action.HOLD, owns=True) == [
        "BUY MORE at +3 or above",
        "REDUCE at -2 or below",
    ]
    assert boundaries(Action.BUY_MORE, owns=True) == ["HOLD at +2 or below"]
    assert boundaries(Action.AVOID, owns=False) == ["WAIT at -1 or above"]


def test_trend_reads_the_average_and_the_year() -> None:
    today = date(2026, 10, 30)
    candles = [
        Candle(today - timedelta(days=400 - d), c, c, c, c, 1)
        for d, c in enumerate(100.0 + d * 0.25 for d in range(400))
    ]
    found = trend(candles, 150.0, today)
    assert found is not None and found.average is not None
    assert found.average > 150.0  # the last 200 closes average about 175
    texts = [(s.points, s.text) for s in found.signals]
    assert texts[0][0] == -1 and "below its 200-day average" in texts[0][1]
    assert texts[1][0] == +1 and texts[1][1].startswith("Up ")


def test_numbers_in_a_note_must_come_from_the_brief() -> None:
    brief = "Price ₹682.50, gain ₹8,250.00 (+13.75%), ROE 13.75%, Mar 2026"
    assert numbers_check("At ₹682.50 you are up ₹8,250 (13.8%) since 2026.", brief)
    assert numbers_check("Hold for 12 months or 3 years.", brief)
    assert not numbers_check("I see it reaching ₹800.", brief)
    assert not numbers_check("Up 14.5%.", brief)
