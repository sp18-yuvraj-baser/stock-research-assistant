"""Turn the evidence behind a holding review into a call.

Someone who owns the stock gets buy more, hold, reduce or sell; someone who
does not gets buy, wait or avoid. The call is a fixed scoring of signals
computed in code, so the same data always gives the same call and every
signal that moved it is shown. The model only puts the call into words.

Each signal belongs to one of four groups (valuation, quality, growth,
trend) and each group's total is capped, so three valuation ratios cannot
outvote everything else. Too few signals and the call stays at hold (or
wait) with low conviction rather than guessing.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from sra.agent.history import price_stats
from sra.figures import rupees
from sra.upstox.market import Candle


class Action(StrEnum):
    BUY_MORE = "buy more"
    HOLD = "hold"
    REDUCE = "reduce"
    SELL = "sell"
    BUY = "buy"
    WAIT = "wait"
    AVOID = "avoid"


GROUP_CAP = {"valuation": 2, "quality": 2, "growth": 2, "trend": 2}
MAX_SCORE = sum(GROUP_CAP.values())
# Fewer signals than this is not enough evidence to move off hold or wait.
MIN_SIGNALS = 3
# A 1-year price move beyond this either way counts as a trend signal.
YEAR_MOVE_PCT = 15.0
AVERAGE_DAYS = 200


@dataclass(frozen=True)
class Signal:
    text: str
    points: int  # positive supports owning the stock, negative argues against
    group: str


@dataclass(frozen=True)
class Verdict:
    action: Action
    conviction: str  # "high", "medium" or "low"
    score: int
    signals: list[Signal] = field(default_factory=list)

    @property
    def favour(self) -> list[Signal]:
        return [s for s in self.signals if s.points > 0]

    @property
    def against(self) -> list[Signal]:
        return [s for s in self.signals if s.points < 0]

    @property
    def headline(self) -> str:
        return (
            f"My call: {self.action.upper()} ({self.conviction} conviction, "
            f"score {self.score:+d} on a scale of -{MAX_SCORE} to +{MAX_SCORE})"
        )


# (lowest score for the action, action), best first.
OWNER_LADDER = (
    (3, Action.BUY_MORE),
    (-1, Action.HOLD),
    (-3, Action.REDUCE),
    (-MAX_SCORE, Action.SELL),
)
BUYER_LADDER = ((3, Action.BUY), (-1, Action.WAIT), (-MAX_SCORE, Action.AVOID))


def _ladder(owns: bool) -> tuple[tuple[int, Action], ...]:
    return OWNER_LADDER if owns else BUYER_LADDER


def decide(signals: list[Signal], *, owns: bool) -> Verdict:
    """The call these signals add up to, for an owner or a would-be buyer."""
    score = 0
    for group, cap in GROUP_CAP.items():
        total = sum(s.points for s in signals if s.group == group)
        score += max(-cap, min(cap, total))
    counted = [s for s in signals if s.points]
    if len(counted) < MIN_SIGNALS:
        return Verdict(Action.HOLD if owns else Action.WAIT, "low", score, signals)
    action = next(a for floor, a in _ladder(owns) if score >= floor)
    groups = {s.group for s in counted}
    if len(groups) < 3:
        conviction = "low"
    elif len(groups) == len(GROUP_CAP) and abs(score) >= 5:
        conviction = "high"
    else:
        conviction = "medium"
    return Verdict(action, conviction, score, signals)


def boundaries(action: Action, *, owns: bool) -> list[str]:
    """The scores at which this call would change, e.g. ["BUY MORE at +3 or
    above", "REDUCE at -2 or below"]."""
    steps = _ladder(owns)
    index = next(i for i, (_, a) in enumerate(steps) if a is action)
    found = []
    if index > 0:
        floor, better = steps[index - 1]
        found.append(f"{better.upper()} at {floor:+d} or above")
    if index < len(steps) - 1:
        found.append(
            f"{steps[index + 1][1].upper()} at {steps[index][0] - 1:+d} or below"
        )
    return found


@dataclass(frozen=True)
class Trend:
    price: float
    average: float | None  # AVERAGE_DAYS-day average close
    year_return: float | None  # %
    high: float
    low: float

    @property
    def signals(self) -> list[Signal]:
        found = []
        if self.average:
            above = self.price >= self.average
            found.append(
                Signal(
                    f"Price {rupees(self.price)} is {'above' if above else 'below'} "
                    f"its {AVERAGE_DAYS}-day average of {rupees(self.average)}",
                    1 if above else -1,
                    "trend",
                )
            )
        if self.year_return is not None and abs(self.year_return) >= YEAR_MOVE_PCT:
            up = self.year_return > 0
            found.append(
                Signal(
                    f"{'Up' if up else 'Down'} {abs(self.year_return):.2f}% over the "
                    "past year",
                    1 if up else -1,
                    "trend",
                )
            )
        return found


def trend(candles: list[Candle], price: float, today: date) -> Trend | None:
    """Price trend from a year of daily candles; None without enough history."""
    stats = price_stats(candles, price, today)
    if stats is None:
        return None
    closes = [c.close for c in candles if c.day < today][-AVERAGE_DAYS:]
    average = sum(closes) / len(closes) if len(closes) == AVERAGE_DAYS else None
    year = next((total for label, total, _ in stats.returns if label == "1 year"), None)
    return Trend(price, average, year, stats.high.price, stats.low.price)
