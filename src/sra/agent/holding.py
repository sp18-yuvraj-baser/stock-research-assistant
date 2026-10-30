"""Answer buy/sell/hold questions with a holding review rendered in code.

A live run of the model-driven version showed why this is code: the local
model multiplied share counts in its head, wrote SQL that failed on the
NSE/BSE duplicate rows, called a P/E above its sector "below", and ran out of
output tokens before the disclaimer. Here every figure comes from a query or
the live quote, every comparison and multiplication is done in Python, and the
"not advice" line cannot be dropped. The review never says keep or sell; it
lays out the evidence on both sides and the questions only the user can
answer.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sra.agent.companies import resolve_companies
from sra.agent.history import price_stats
from sra.agent.loop import PathResult
from sra.figures import months_between, pct_change, period_start, rupees, to_decimal
from sra.tools.get_live_quote import LiveQuote, get_live_quote, ist_time
from sra.tools.run_sql import SqlResult, run_sql
from sra.upstox.client import UpstoxError
from sra.upstox.market import daily_candles

DISCLAIMER = (
    "This is research, not investment advice. For a personal recommendation, "
    "consult a SEBI-registered investment adviser."
)

NO_COMPANY = f"""\
I couldn't find an NSE company in that question. I can't pick stocks or tell
you to buy, sell or hold, but I can review one.
Name the NSE/BSE company, e.g. "I have 100 HDFCBANK shares bought at 1500,
should I keep them?", and I'll show its live price, your position value,
valuation against its sector and its profit trend.

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


def _ratios(result: SqlResult) -> list[_Ratio]:
    ratios = []
    for row in result.rows:
        company = to_decimal(row["company_value"])
        if company is not None:
            ratios.append(
                _Ratio(str(row["name"]), company, to_decimal(row["sector_value"]))
            )
    order = {name: index for index, name in enumerate(_RATIO_ORDER)}
    return sorted(ratios, key=lambda r: order.get(r.name, len(order)))


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


def _year_lines(key: str, price: float) -> list[str]:
    """52-week range and 1-year return; nothing when history is unavailable,
    since the review stands without them."""
    try:
        stats = price_stats(daily_candles(key, years=1), price, date.today())
    except UpstoxError:
        return []
    if stats is None:
        return []
    lines = [
        f"52-week range: {rupees(stats.low.price)} to {rupees(stats.high.price)} "
        f"(price is {100 * (1 - price / stats.high.price):.1f}% below the high)"
    ]
    year = next((total for label, total, _ in stats.returns if label == "1 year"), None)
    if year is not None:
        lines.append(f"1-year price return: {year:+.2f}%")
    return lines


def _position_lines(position: Position, quote: LiveQuote) -> list[str]:
    if quote.last_price is None or quote.last_price <= 0:
        return [f"Live price unavailable: {quote.error or 'no valid price returned'}."]
    price = Decimal(str(quote.last_price))
    exchange = quote.exchange.removesuffix("_EQ") or "NSE"
    lines = [f"Live price: {rupees(price)} ({exchange}, {ist_time(quote.fetched_at)})"]
    lines += _year_lines(quote.instrument_key, quote.last_price)
    if position.conflicting:
        lines.append(
            "Position value left out: the question gives more than one share "
            "count or price."
        )
    if position.quantity is None:
        return lines
    value = position.quantity * price
    lines.append(f"Your {position.quantity.normalize():f} shares: {rupees(value)}")
    if position.buy_price is not None:
        cost = position.quantity * position.buy_price
        gain = value - cost
        word = "gain" if gain >= 0 else "loss"
        lines.append(
            f"Bought at {rupees(position.buy_price)}: unrealised {word} "
            f"{rupees(abs(gain))} ({pct_change(cost, value):+}%)"
        )
        # The live price is post-adjustment; a pre-bonus buy price is not, so
        # a 1:1 bonus alone shows as a 50% loss.
        lines.append(
            "If you bought before a bonus issue or stock split, divide your buy "
            "price by the adjustment ratio (and multiply your share count) first; "
            "otherwise this gain or loss is wrong."
        )
    return lines


def _points(ratios: list[_Ratio], series: list[_Series]) -> tuple[list[str], list[str]]:
    favour: list[str] = []
    against: list[str] = []
    for ratio in ratios:
        if ratio.meaningless:
            against.append(_describe_ratio(ratio))
        elif ratio.standing:
            (favour if ratio.standing > 0 else against).append(_describe_ratio(ratio))
    for item in series:
        move = item.latest_move
        if move is not None:
            (favour if move[1] > 0 else against).append(move[0])
    return favour, against


def _with_disclaimer(text: str) -> PathResult:
    return PathResult(text=f"{text}\n\n{DISCLAIMER}")


def review(question: str) -> PathResult:
    """Render a review of the one company the question names."""
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
            'e.g. "Should I keep my 10 TCS shares?".'
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

    intro = "I can't tell you to buy, sell or hold, but here is what the data says."
    # No sector until migration 009 and an ingest have run; the review stands
    # without it.
    if not profile_sql.error and profile_sql.rows and profile_sql.rows[0]["sector"]:
        intro = f"Sector: {profile_sql.rows[0]['sector']}\n{intro}"
    if resolution.set_aside:
        others = ", ".join(f"{c.ticker} ({c.name})" for c in resolution.set_aside)
        intro += f"\nAlso matched {others}: ask separately if you meant it."
    sections = [
        f"{company.name} ({company.ticker}): review\n{intro}",
        "Your position\n"
        + "\n".join(f"  {line}" for line in _position_lines(position, quote)),
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
                "like-for-like growth."
            )
        sections.append(f"{heading}\n" + "\n".join(lines))
    else:
        sections.append(f"{heading}\n  No yearly revenue or profit data.")

    favour, against = _points(ratios, series)
    nothing = ["Nothing in this data stands out."]
    sections.append(
        "Points in favour\n" + "\n".join(f"  - {p}" for p in favour or nothing)
    )
    sections.append(
        "Points against\n" + "\n".join(f"  - {p}" for p in against or nothing)
    )
    sections.append(
        "Before deciding, ask yourself\n"
        "  - What did you pay and how long have you held? Listed shares held "
        "over 12 months are taxed as long-term capital gains, under 12 months "
        "as short-term.\n"
        "  - How much of your portfolio is this one stock?\n"
        "  - Does your original reason for buying still hold, and what is your "
        "time horizon?\n"
        "  - Not covered here: news, 52-week range, technicals, analyst views."
    )
    sections.append(DISCLAIMER)
    result.text = "\n\n".join(sections)
    return result
