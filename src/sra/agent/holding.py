"""Answer buy/sell/hold questions with a researched call.

The evidence is gathered and scored in code: the live quote, a year of
prices, stored ratios against the sector and the yearly results. A live run
of an earlier model-driven version showed why: the local model multiplied
share counts in its head, wrote SQL that failed on the NSE/BSE duplicate
rows and called a P/E above its sector "below". So every figure comes from a
query or the live quote, the call comes from verdict.decide, and the model
only writes the analyst's note around it (narrate checks that note and falls
back to prose written here). The evidence is shown under the note, and the
disclaimer cannot be dropped.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.agent.narrate import narrate
from sra.agent.universe import CompanyFacts
from sra.agent.verdict import (
    AVERAGE_DAYS,
    MAX_SCORE,
    MIN_SIGNALS,
    Action,
    Signal,
    Trend,
    Verdict,
    boundaries,
    decide,
    trend,
)
from sra.figures import (
    months_between,
    pct_change,
    period_label,
    period_start,
    rupees,
    to_decimal,
)
from sra.tools.get_live_quote import LiveQuote, get_live_quote, ist_time
from sra.tools.run_sql import SqlResult, run_sql
from sra.upstox.client import UpstoxError
from sra.upstox.market import daily_candles

DISCLAIMER = (
    "This is research, not investment advice: the call comes from a fixed "
    "scoring of the data shown and does not know your goals, tax position or "
    "other holdings. For a personal recommendation, consult a SEBI-registered "
    "investment adviser."
)

NO_COMPANY = f"""\
I couldn't find an NSE company in that question. Name the NSE/BSE company,
e.g. "I have 100 HDFCBANK shares bought at 1500, should I keep them?" or
"Should I buy TCS?", and I'll research it and give you my call: buy more,
hold, reduce or sell if you own it; buy, wait or avoid if you don't.

{DISCLAIMER}"""


_NUMBER = r"(\d[\d,]{0,15}(?:\.\d{1,4})?)"
_CURRENCY = r"(?:rs\.?|inr|₹)?\s*"
_QUANTITY = (
    # "100 shares", "100 hdfc bank stocks"; never "50000 worth of ... shares".
    re.compile(
        r"(\d[\d,]{0,15})\s+(?:(?!worth\b|rupees\b|in\b)[a-z&.'-]+\s+){0,4}?"
        r"(?:shares?|stocks?|qty)\b",
        re.I,
    ),
    re.compile(r"\b(?:bought|own|hold|holding|have)\s+(\d[\d,]{0,15})", re.I),
    re.compile(r"\b(?:qty|quantity)\s*(?:of|is|:)?\s*(\d[\d,]{0,15})", re.I),
)
_BUY_PRICE = (
    re.compile(
        r"(?:\bbought\b[^.?!]{0,60}?\bat\b|\b(?:avg\.?|average)(?: (?:buy )?price"
        r"| cost)?|\bbuy(?:ing)? price|\bcost(?: price)?|\bpaid|@)\s*"
        rf"(?:of|is|was|:)?\s*{_CURRENCY}{_NUMBER}",
        re.I,
    ),
    re.compile(
        rf"\bshares?\b[^.?!]{{0,40}}?(?<!trading )\bat\s*{_CURRENCY}{_NUMBER}", re.I
    ),
    re.compile(rf"{_NUMBER}\s*(?:avg|average|cost)\b", re.I),
)
# A number that is money or a portfolio count, not this stock's share count:
# "2.5 lakh", "Rs 50,000", "50000 invested in TCS", "30 stocks in my portfolio".
_NOT_SHARES_AFTER = re.compile(
    r"\s*(?:\.\d|lakhs?\b|lacs?\b|crores?\b|cr\b|k\b|%|rupees\b|rs\b|inr\b|worth\b"
    r"|invested\b|in\b|into\b|of\b"
    r"|(?:\w+\s+){0,2}?(?:in|across) (?:my|the|your|our) portfolio)",
    re.I,
)
_NOT_SHARES_BEFORE = re.compile(r"(?:rs\.?|inr|₹)\s*$", re.I)
_NOT_PRICE_AFTER = re.compile(r"\s*(?:lakhs?\b|lacs?\b|crores?\b|cr\b|k\b|%)", re.I)

# For these a lower figure than the sector is the better one.
_LOWER_IS_BETTER = frozenset({"P/E", "P/B", "EV/EBITDA", "Net NPA"})
_VALUATION = frozenset({"P/E", "P/B", "EV/EBITDA"})
_PERCENT = frozenset({"ROE", "ROA", "ROCE", "NIM", "Net NPA", "CASA"})
_RATIO_ORDER = (
    "P/E", "P/B", "EV/EBITDA", "ROE", "ROCE", "ROA",
    "NIM", "Net NPA", "CASA", "Quick Ratio",
)  # fmt: skip

# A sector benchmark more than this many times off the company's own figure
# is a data problem, not a comparison (HDFCBANK P/E 13.01 vs "sector" 0.32).
_IMPLAUSIBLE_GAP = Decimal(10)
# Year-on-year moves beyond this usually mean a merger, demerger or one-off.
_STEP_CHANGE_PCT = Decimal(50)
_TREND_PERIODS = 3


@dataclass(frozen=True)
class Position:
    quantity: Decimal | None = None
    buy_price: Decimal | None = None
    # Several different share counts or prices: which belongs to which
    # company cannot be told, so neither is used.
    conflicting: bool = False


def _numbers(
    patterns: tuple[re.Pattern[str], ...],
    question: str,
    *,
    rejects_after: re.Pattern[str],
    rejects_before: re.Pattern[str] | None = None,
) -> set[Decimal]:
    found: set[Decimal] = set()
    for pattern in patterns:
        for match in pattern.finditer(question):
            if rejects_after.match(question, match.end(1)):
                continue
            if rejects_before and rejects_before.search(question[: match.start(1)]):
                continue
            value = to_decimal(match.group(1))
            if value is not None and value > 0:
                found.add(value)
    return found


def parse_position(question: str) -> Position:
    """Share count and buy price as the user stated them, if they did."""
    quantities = _numbers(
        _QUANTITY,
        question,
        rejects_after=_NOT_SHARES_AFTER,
        rejects_before=_NOT_SHARES_BEFORE,
    )
    prices = _numbers(_BUY_PRICE, question, rejects_after=_NOT_PRICE_AFTER)
    if len(quantities) > 1 or len(prices) > 1:
        return Position(conflicting=True)
    return Position(
        quantity=next(iter(quantities), None), buy_price=next(iter(prices), None)
    )


def _figure(name: str, value: Decimal) -> str:
    return f"{value.normalize():f}{'%' if name in _PERCENT else ''}"


@dataclass(frozen=True)
class _Ratio:
    name: str
    company: Decimal
    sector: Decimal | None

    @property
    def meaningless(self) -> bool:
        """A negative or zero multiple says losses, not cheapness."""
        return self.name in _VALUATION and self.company <= 0

    @property
    def standing(self) -> int | None:
        """+1 better than sector, -1 worse, 0 in line, None not comparable."""
        if self.meaningless or self.sector is None:
            return None
        # A sector average at or below zero (IT "ROA -2.04") is bad data;
        # only Net NPA can genuinely be zero.
        if self.sector <= 0 and self.name != "Net NPA":
            return None
        if self.company > 0 and self.sector > 0:
            high, low = max(self.company, self.sector), min(self.company, self.sector)
            if high / low > _IMPLAUSIBLE_GAP:
                return None
        if self.company == self.sector:
            return 0
        better = (
            self.company < self.sector
            if self.name in _LOWER_IS_BETTER
            else self.company > self.sector
        )
        return 1 if better else -1


def _ordered(ratios: list[_Ratio]) -> list[_Ratio]:
    order = {name: index for index, name in enumerate(_RATIO_ORDER)}
    return sorted(ratios, key=lambda r: order.get(r.name, len(order)))


def _ratios(result: SqlResult) -> list[_Ratio]:
    ratios = []
    for row in result.rows:
        company = to_decimal(row["company_value"])
        if company is not None:
            ratios.append(
                _Ratio(str(row["name"]), company, to_decimal(row["sector_value"]))
            )
    return _ordered(ratios)


def _describe_ratio(ratio: _Ratio) -> str:
    own = f"{ratio.name} {_figure(ratio.name, ratio.company)}"
    if ratio.meaningless:
        return f"{own}: not meaningful, usually a sign of losses or negative net worth"
    if ratio.sector is None:
        return f"{own} (no sector figure)"
    versus = f"{own} vs sector {_figure(ratio.name, ratio.sector)}"
    standing = ratio.standing
    if standing is None:
        return f"{versus} (sector figure looks unreliable)"
    if standing == 0:
        return f"{versus}: in line with sector"
    if ratio.name in _VALUATION:
        return f"{versus}: {'cheaper' if standing > 0 else 'pricier'} than sector"
    note = " (lower is better)" if ratio.name in _LOWER_IS_BETTER else ""
    return f"{versus}: {'better' if standing > 0 else 'weaker'} than sector{note}"


def _when(period: str) -> date:
    """Sort key for a label already known to parse (see _series)."""
    parsed = period_start(period)
    assert parsed is not None
    return parsed


@dataclass(frozen=True)
class _Series:
    label: str
    points: list[tuple[str, Decimal]]  # oldest first

    def _is_step(self, index: int) -> bool:
        prior, current = self.points[index - 1][1], self.points[index][1]
        return (
            prior > 0
            and current > 0
            and abs(pct_change(prior, current)) > _STEP_CHANGE_PCT
        )

    @property
    def step_changes(self) -> list[str]:
        return [
            self.points[i][0] for i in range(1, len(self.points)) if self._is_step(i)
        ]

    @property
    def latest_move(self) -> tuple[str, int] | None:
        """The latest change in words, and +1/-1 for which side it supports.

        None when there is nothing to compare or the move is a step change
        that would mislead as a growth rate.
        """
        if len(self.points) < 2 or self._is_step(len(self.points) - 1):
            return None
        (prior_period, prior), (period, current) = self.points[-2:]
        months = months_between(_when(prior_period), _when(period))
        span = (
            f"in the year to {period}"
            if months == 12
            else f"from {prior_period} to {period}"
        )
        if prior > 0 and current > 0:
            change = pct_change(prior, current)
            if change == 0:
                return None
            verb = "grew" if change > 0 else "fell"
            return f"{self.label} {verb} {abs(change)}% {span}", 1 if change > 0 else -1
        if prior <= 0 < current:
            return f"{self.label} went from a loss to a profit {span}", 1
        if current <= 0 < prior:
            return f"{self.label} went from a profit to a loss {span}", -1
        if current == prior:
            return None
        verb = "narrowed" if current > prior else "widened"
        return f"{self.label} loss {verb} {span}", 1 if current > prior else -1


def _series(result: SqlResult) -> list[_Series]:
    by_category: dict[str, list[tuple[str, Decimal]]] = {}
    for row in result.rows:
        value = to_decimal(row["value"])
        if value is not None and period_start(str(row["period"])) is not None:
            by_category.setdefault(str(row["category"]), []).append(
                (str(row["period"]), value)
            )
    return _series_from(by_category)


def _series_from(by_category: dict[str, list[tuple[str, Decimal]]]) -> list[_Series]:
    labels = {"revenue": "Revenue", "net_profit": "Net profit"}
    series = []
    for category, label in labels.items():
        points = sorted(by_category.get(category, []), key=lambda p: _when(p[0]))
        # One extra period so the oldest shown still has a change to compare.
        if points:
            series.append(_Series(label, points[-(_TREND_PERIODS + 1) :]))
    return series


def _describe_series(series: _Series) -> str:
    shown = series.points[-_TREND_PERIODS:]
    path = " → ".join(f"{period} {value:,.2f}" for period, value in shown)
    return f"{series.label}: {path}"


def _signals(ratios: list[_Ratio], series: list[_Series]) -> list[Signal]:
    """Valuation, quality and growth signals; the price trend adds its own."""
    found = []
    for ratio in ratios:
        group = "valuation" if ratio.name in _VALUATION else "quality"
        if ratio.meaningless:
            # A negative multiple means losses, worse than any gap to sector.
            found.append(Signal(_describe_ratio(ratio), -2, group))
        elif ratio.standing:
            found.append(Signal(_describe_ratio(ratio), ratio.standing, group))
    for item in series:
        move = item.latest_move
        if move is not None:
            found.append(Signal(move[0], move[1], "growth"))
    return found


def facts_signals(company: CompanyFacts) -> list[Signal]:
    """The same signals from stored facts, for the portfolio review."""
    ratios = [
        _Ratio(name, own, sector)
        for name, (own, sector) in company.ratios.items()
        if own is not None
    ]
    by_category = {
        category: [(period_label(when), value) for when, value in points]
        for category, points in company.yearly.items()
    }
    return _signals(_ordered(ratios), _series_from(by_category))


def price_trend(key: str, price: float) -> Trend | None:
    """A year of price trend; None when history is unavailable, since the
    review stands without it."""
    try:
        return trend(daily_candles(key, years=1), price, date.today())
    except UpstoxError:
        return None


def _trend_lines(found: Trend) -> list[str]:
    lines = [
        f"52-week range: {rupees(found.low)} to {rupees(found.high)} "
        f"(price is {100 * (1 - found.price / found.high):.1f}% below the high)"
    ]
    if found.average:
        lines.append(f"{AVERAGE_DAYS}-day average: {rupees(found.average)}")
    if found.year_return is not None:
        lines.append(f"1-year price return: {found.year_return:+.2f}%")
    return lines


@dataclass(frozen=True)
class _Stake:
    quantity: Decimal
    value: Decimal
    gain: Decimal | None = None  # None without a buy price
    gain_pct: Decimal | None = None


def _stake(position: Position, price: Decimal) -> _Stake | None:
    if position.quantity is None:
        return None
    value = position.quantity * price
    if position.buy_price is None:
        return _Stake(position.quantity, value)
    cost = position.quantity * position.buy_price
    return _Stake(position.quantity, value, value - cost, pct_change(cost, value))


def _price(quote: LiveQuote) -> Decimal | None:
    if quote.last_price is None or quote.last_price <= 0:
        return None
    return Decimal(str(quote.last_price))


def _position_lines(
    position: Position, quote: LiveQuote, found: Trend | None
) -> list[str]:
    price = _price(quote)
    if price is None:
        return [f"Live price unavailable: {quote.error or 'no valid price returned'}."]
    exchange = quote.exchange.removesuffix("_EQ") or "NSE"
    lines = [f"Live price: {rupees(price)} ({exchange}, {ist_time(quote.fetched_at)})"]
    if found is not None:
        lines += _trend_lines(found)
    if position.conflicting:
        lines.append(
            "Position value left out: the question gives more than one share "
            "count or price."
        )
    stake = _stake(position, price)
    if stake is None:
        return lines
    lines.append(f"Your {stake.quantity.normalize():f} shares: {rupees(stake.value)}")
    if stake.gain is not None and position.buy_price is not None:
        word = "gain" if stake.gain >= 0 else "loss"
        lines.append(
            f"Bought at {rupees(position.buy_price)}: unrealised {word} "
            f"{rupees(abs(stake.gain))} ({stake.gain_pct:+}%)"
        )
    return lines


# The live price is post-adjustment; a pre-bonus buy price is not, so a 1:1
# bonus alone shows as a 50% loss.
_ADJUSTMENT_NOTE = (
    "If you bought before a bonus issue or stock split, divide your buy price "
    "by the adjustment ratio (and multiply your share count) first; otherwise "
    "this gain or loss is wrong."
)

# Owning the stock is what separates "buy more / sell" from "buy / avoid".
_OWNS = re.compile(
    r"\b(my|i have|i hold|i own|i've|holding|keep|sell|selling|exit|bought"
    r"|purchased|buy more|add more|average down)\b",
    re.IGNORECASE,
)


def owns_stock(question: str, position: Position) -> bool:
    return (
        position.quantity is not None
        or position.buy_price is not None
        or bool(_OWNS.search(question))
    )


def _lower_first(text: str) -> str:
    """'Price ₹10 is ...' -> 'price ₹10 is ...', but 'P/E 12' stays."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _watch(
    verdict: Verdict, found: Trend | None, series: list[_Series], owns: bool
) -> list[str]:
    """What would move the call, in the terms the call was made in."""
    if len([s for s in verdict.signals if s.points]) < MIN_SIGNALS:
        lines = [
            "More data: too few signals to move off "
            f"{verdict.action.upper()}; refreshing fundamentals may add some"
        ]
    else:
        lines = [
            "The score: the call becomes "
            + " and ".join(boundaries(verdict.action, owns=owns))
        ]
    if found is not None and found.average:
        above = found.price >= found.average
        lines.append(
            f"A close {'below' if above else 'back above'} the {AVERAGE_DAYS}-day "
            f"average of {rupees(found.average)}, which would turn the trend "
            f"{'against' if above else 'in favour of'} the stock"
        )
    latest = max((s.points[-1][0] for s in series), key=_when, default=None)
    if latest is not None:
        lines.append(
            f"The yearly results after {latest}: growth in revenue and net profit "
            "counts for the stock, a fall counts against it"
        )
    return lines


_ACTION_TEXT = {
    Action.BUY_MORE: "add to your position",
    Action.HOLD: "hold what you have, without adding at this price",
    Action.REDUCE: "reduce your position",
    Action.SELL: "sell",
    Action.BUY: "buy",
    Action.WAIT: "wait rather than buy right now",
    Action.AVOID: "avoid it for now",
}
_POSITION_TEXT = {
    Action.BUY_MORE: "If you add, do it in steps rather than all at once.",
    Action.HOLD: "Nothing here argues for selling, and not enough for adding.",
    Action.REDUCE: "Selling part locks in some of the result and lowers what rides "
    "on this one stock.",
    Action.SELL: "Check the tax before you sell: shares held over 12 months are "
    "taxed as long-term gains, and a loss can be set off against other gains.",
}


def _plain_note(name: str, verdict: Verdict, stake: _Stake | None) -> str:
    """The note in code, for when the model is down or its note fails checks."""
    first = (
        f"My call on {name} is to {_ACTION_TEXT[verdict.action]}, with "
        f"{verdict.conviction} conviction."
    )
    for word, signals in (("for", verdict.favour), ("against", verdict.against)):
        if signals:
            texts = "; ".join(_lower_first(s.text) for s in signals[:3])
            first += f" Working {word} it: {texts}."
    paragraphs = [first]
    second = ""
    if stake is not None:
        second = f"Your {stake.quantity.normalize():f} shares are worth "
        second += rupees(stake.value)
        if stake.gain is not None:
            word = "a gain" if stake.gain >= 0 else "a loss"
            second += (
                f", {word} of {rupees(abs(stake.gain))} ({stake.gain_pct:+}%) on "
                "what you paid"
            )
        second += ". "
    if verdict.action in _POSITION_TEXT:
        second += _POSITION_TEXT[verdict.action]
    if second:
        paragraphs.append(second.strip())
    return "\n\n".join(paragraphs)


def _brief(
    question: str,
    title: str,
    owns: bool,
    position_lines: list[str],
    verdict: Verdict,
) -> str:
    """Everything the model may say, and nothing else."""

    def listed(heading: str, items: list[str]) -> list[str]:
        return [heading, *(f"- {item}" for item in items or ["none"])]

    return "\n".join(
        [
            f"Client's question: {question}",
            f"Stock: {title}",
            "The client owns this stock."
            if owns
            else "The client does not own this stock and asks whether to buy it.",
            f"Call: {verdict.action.upper()}, {verdict.conviction} conviction "
            f"(score {verdict.score:+d} on a scale of -{MAX_SCORE} to +{MAX_SCORE})",
            *listed("Price and position:", position_lines),
            *listed("Evidence for:", [s.text for s in verdict.favour]),
            *listed("Evidence against:", [s.text for s in verdict.against]),
        ]
    )


def _with_disclaimer(text: str) -> PathResult:
    return PathResult(text=f"{text}\n\n{DISCLAIMER}")


def review(question: str) -> PathResult:
    """Research the one company the question names and make a call on it."""
    resolution = resolve_companies(question)
    if resolution.error:
        return _with_disclaimer(
            f"I couldn't look up the company just now ({resolution.error}). "
            "Please try again."
        )
    if len(resolution.companies) > 1:
        names = " and ".join(c.ticker for c in resolution.companies)
        return _with_disclaimer(
            f"You mentioned {names}. I review one stock at a time so every "
            "figure stays tied to the right company: ask about each separately, "
            'e.g. "Should I keep my 10 TCS shares?", or list them all as a '
            'portfolio: "100 TCS at 3500, 50 INFY at 1400".'
        )
    if not resolution.companies:
        if not resolution.ambiguous:
            return PathResult(text=NO_COMPANY)
        names = "; ".join(f"{c.ticker} ({c.name})" for c in resolution.ambiguous)
        return _with_disclaimer(
            f"Which company do you mean: {names}? Ask again with the NSE symbol."
        )
    company = resolution.companies[0]

    result = PathResult()
    position = parse_position(question)
    owns = owns_stock(question, position)
    quote = get_live_quote(company.ticker, exchange=company.exchange)
    result.quote_calls.append(quote)
    params = {"key": company.instrument_key}
    ratios_sql = run_sql(
        "SELECT name, company_value, sector_value, fetched_at "
        "FROM fundamental_ratios WHERE instrument_key = %(key)s",
        params,
    )
    financials_sql = run_sql(
        "SELECT category, period, value FROM fundamental_financials "
        "WHERE instrument_key = %(key)s "
        "AND statement = 'income_statement' AND time_period = 'yearly' "
        "AND category IN ('revenue', 'net_profit')",
        params,
    )
    profile_sql = run_sql(
        "SELECT sector FROM company_profiles WHERE instrument_key = %(key)s", params
    )
    result.sql_calls.extend([ratios_sql, financials_sql, profile_sql])
    ratios = _ratios(ratios_sql)
    series = _series(financials_sql)
    price = _price(quote)
    found = None if price is None else price_trend(quote.instrument_key, float(price))

    signals = _signals(ratios, series) + ([] if found is None else found.signals)
    verdict = decide(signals, owns=owns)
    watch = _watch(verdict, found, series, owns)
    position_lines = _position_lines(position, quote, found)
    stake = None if price is None else _stake(position, price)

    title = f"{company.name} ({company.ticker})"
    lede = f"{verdict.headline}."
    # No sector until migration 009 and an ingest have run; the review stands
    # without it.
    if not profile_sql.error and profile_sql.rows and profile_sql.rows[0]["sector"]:
        sector = profile_sql.rows[0]["sector"]
        lede += f" Sector: {sector}."
        title += f", sector {sector}"
    if resolution.set_aside:
        others = ", ".join(f"{c.ticker} ({c.name})" for c in resolution.set_aside)
        lede += f"\nAlso matched {others}: ask separately if you meant it."
    note = narrate(
        _brief(question, title, owns, position_lines, verdict), verdict.action
    ) or _plain_note(company.name, verdict, stake)

    sections = [
        f"{company.name} ({company.ticker}): review\n{lede}",
        note,
        "Price and position\n"
        + "\n".join(
            f"  {line}"
            for line in position_lines
            + ([_ADJUSTMENT_NOTE] if stake and stake.gain is not None else [])
        ),
    ]
    heading = "Valuation and quality vs sector"
    if ratios_sql.error:
        sections.append(f"{heading}\n  Data lookup failed: {ratios_sql.error}")
    elif ratios:
        fetched = ist_time(ratios_sql.rows[0]["fetched_at"])
        sections.append(
            f"{heading} (as of {fetched})\n"
            + "\n".join(f"  {_describe_ratio(r)}" for r in ratios)
        )
    else:
        sections.append(f"{heading}\n  No ratio data for this company.")
    heading = "Yearly results (INR crore)"
    if financials_sql.error:
        sections.append(f"{heading}\n  Data lookup failed: {financials_sql.error}")
    elif series:
        lines = [f"  {_describe_series(s)}" for s in series]
        steps = sorted({p for s in series for p in s.step_changes}, key=_when)
        if steps:
            lines.append(
                f"  Note: a move of over {_STEP_CHANGE_PCT}% in {', '.join(steps)} "
                "often reflects a merger, demerger or one-off item, not "
                "like-for-like growth, so it is left out of the score."
            )
        sections.append(f"{heading}\n" + "\n".join(lines))
    else:
        sections.append(f"{heading}\n  No yearly revenue or profit data.")

    for heading, side in (
        ("Points in favour", verdict.favour),
        ("Points against", verdict.against),
    ):
        points = [s.text for s in side] or ["Nothing in this data stands out."]
        sections.append(f"{heading}\n" + "\n".join(f"  - {p}" for p in points))
    sections.append(
        "What would change my view\n" + "\n".join(f"  - {line}" for line in watch)
    )
    sections.append(DISCLAIMER)
    result.text = "\n\n".join(sections)
    return result
