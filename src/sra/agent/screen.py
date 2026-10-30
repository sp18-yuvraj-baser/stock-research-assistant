"""Answer "suggest some stocks" with a transparent fundamentals screen.

Like the holding review this is rendered in code, not by the model: every
company listed passed the same stated tests on stored Upstox data, and every
figure shown was computed here. It is a starting point for research, never a
buy list, and it says so; no screen of past figures can promise a return.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from sra.agent.loop import PathResult
from sra.db import connect
from sra.figures import (
    months_between,
    pct_change,
    period_label,
    period_start,
    to_decimal,
)
from sra.tools.get_live_quote import ist_time
from sra.tools.run_sql import SqlResult

DISCLAIMER = (
    "This is research, not investment advice. For a personal recommendation, "
    "consult a SEBI-registered investment adviser."
)

# The tests, stated in the answer exactly as applied.
MIN_REVENUE_CRORE = Decimal(5000)  # established companies; no market cap data
GROWTH_YEARS = 3
MIN_ROE = Decimal(15)
MIN_BANK_ROE = Decimal(12)
MAX_BANK_NET_NPA = Decimal(1)
MAX_LIABILITIES_TO_ASSETS = Decimal("0.6")
MAX_PE_TO_SECTOR = Decimal("1.5")
# Used instead when the sector P/E is unusable (zero, negative or wildly off).
MAX_PE_WITHOUT_SECTOR = Decimal(60)
# Year-on-year jumps beyond this are mergers or one-offs, not steady growth.
MAX_YEARLY_GROWTH_PCT = Decimal(50)
# Same rule as the holding review: a benchmark >10x off is a data problem.
_IMPLAUSIBLE_GAP = Decimal(10)
# Figures older than this describe a different company.
MAX_DATA_AGE_MONTHS = 24
SHOWN = 10

RATIOS_SQL = """
SELECT i.instrument_key, i.ticker, i.name, fr.name AS metric,
       fr.company_value, fr.sector_value, fr.fetched_at
FROM fundamental_ratios fr
JOIN instruments i ON i.instrument_key = fr.instrument_key
WHERE i.exchange = 'NSE_EQ' AND fr.name IN ('ROE', 'P/E', 'NIM', 'Net NPA')
"""

FINANCIALS_SQL = """
SELECT ff.instrument_key, ff.category, ff.period, ff.value
FROM fundamental_financials ff
JOIN instruments i ON i.instrument_key = ff.instrument_key
WHERE i.exchange = 'NSE_EQ' AND ff.time_period = 'yearly'
  AND ff.category IN ('revenue', 'net_profit', 'total_asset', 'total_liability')
"""


@dataclass(frozen=True)
class Pick:
    ticker: str
    name: str
    roe: Decimal
    pe: Decimal
    pe_sector: Decimal | None
    revenue_growth: Decimal  # compound yearly %, over GROWTH_YEARS
    profit_growth: Decimal
    latest_period: str

    @property
    def pe_vs_sector(self) -> Decimal:
        """P/E as a multiple of the sector's; 1 when there is no usable one."""
        return self.pe / self.pe_sector if self.pe_sector else Decimal(1)


def _usable_benchmark(own: Decimal, sector: Decimal | None) -> Decimal | None:
    if sector is None or sector <= 0 or own <= 0:
        return None
    high, low = max(own, sector), min(own, sector)
    return sector if high / low <= _IMPLAUSIBLE_GAP else None


def _cagr(first: Decimal, last: Decimal, years: int) -> Decimal:
    rate = (float(last) / float(first)) ** (1 / years) - 1
    return Decimal(str(round(rate * 100, 1)))


def _steady_growth(points: list[tuple[date, Decimal]]) -> Decimal | None:
    """Compound growth over the last GROWTH_YEARS years if the series is
    positive, a year apart, rising every year and free of step changes."""
    if len(points) < GROWTH_YEARS + 1:
        return None
    recent = points[-(GROWTH_YEARS + 1) :]
    for (earlier, prior), (later, current) in pairwise(recent):
        if months_between(earlier, later) != 12 or prior <= 0 or current <= prior:
            return None
        if pct_change(prior, current) > MAX_YEARLY_GROWTH_PCT:
            return None
    return _cagr(recent[0][1], recent[-1][1], GROWTH_YEARS)


def _passes(
    ratios: dict[str, tuple[Decimal | None, Decimal | None]],
    series: dict[str, list[tuple[date, Decimal]]],
    today: date,
) -> tuple[Decimal, Decimal, Decimal | None, Decimal, Decimal, date] | None:
    """(roe, pe, usable sector pe, revenue growth, profit growth, latest
    period) for a company that passes every test, else None."""
    revenue, profit = series.get("revenue", []), series.get("net_profit", [])
    if not revenue or revenue[-1][1] < MIN_REVENUE_CRORE:
        return None
    latest = revenue[-1][0]
    if months_between(latest, today) > MAX_DATA_AGE_MONTHS:
        return None
    revenue_growth, profit_growth = _steady_growth(revenue), _steady_growth(profit)
    if revenue_growth is None or profit_growth is None:
        return None

    roe, roe_sector = ratios.get("ROE", (None, None))
    is_bank = "NIM" in ratios
    if roe is None or roe < (MIN_BANK_ROE if is_bank else MIN_ROE):
        return None
    sector_roe = _usable_benchmark(roe, roe_sector)
    if sector_roe is not None and roe < sector_roe:
        return None

    if is_bank:
        net_npa = ratios.get("Net NPA", (None, None))[0]
        if net_npa is None or net_npa > MAX_BANK_NET_NPA:
            return None
    else:
        assets = dict(series.get("total_asset", [])).get(latest)
        liabilities = dict(series.get("total_liability", [])).get(latest)
        if not assets or liabilities is None:
            return None
        if liabilities / assets > MAX_LIABILITIES_TO_ASSETS:
            return None

    pe, pe_sector_raw = ratios.get("P/E", (None, None))
    if pe is None or pe <= 0:
        return None
    pe_sector = _usable_benchmark(pe, pe_sector_raw)
    limit = pe_sector * MAX_PE_TO_SECTOR if pe_sector else MAX_PE_WITHOUT_SECTOR
    if pe > limit:
        return None
    return roe, pe, pe_sector, revenue_growth, profit_growth, latest


def _rank(picks: list[Pick]) -> list[Pick]:
    """Order by the average of each pick's rank on ROE (high first), profit
    growth (high first) and P/E against its sector (low first)."""
    orderings = [
        sorted(picks, key=lambda p: -p.roe),
        sorted(picks, key=lambda p: -p.profit_growth),
        sorted(picks, key=lambda p: p.pe_vs_sector),
    ]
    score = {p.ticker: 0 for p in picks}
    for ordering in orderings:
        for position, pick in enumerate(ordering):
            score[pick.ticker] += position
    return sorted(picks, key=lambda p: (score[p.ticker], p.ticker))


Ratio = tuple[Decimal | None, Decimal | None]  # (company, sector)
Series = list[tuple[date, Decimal]]  # oldest first


@dataclass
class _Company:
    ticker: str
    name: str
    ratios: dict[str, Ratio]


def _load() -> tuple[dict[str, _Company], dict[str, dict[str, Series]], object]:
    """Every NSE listing's screening ratios and yearly series, plus the time
    the newest ratio was fetched."""
    companies: dict[str, _Company] = {}
    series: dict[str, dict[str, Series]] = {}
    fetched: list[object] = []
    with connect(readonly=True) as conn:
        for row in conn.execute(RATIOS_SQL).fetchall():
            company = companies.setdefault(
                str(row["instrument_key"]),
                _Company(str(row["ticker"]), str(row["name"]), {}),
            )
            company.ratios[str(row["metric"])] = (
                to_decimal(row["company_value"]),
                to_decimal(row["sector_value"]),
            )
            fetched.append(row["fetched_at"])
        for row in conn.execute(FINANCIALS_SQL).fetchall():
            when, value = period_start(str(row["period"])), to_decimal(row["value"])
            if when is not None and value is not None:
                series.setdefault(str(row["instrument_key"]), {}).setdefault(
                    str(row["category"]), []
                ).append((when, value))
    for by_category in series.values():
        for points in by_category.values():
            points.sort()
    return companies, series, max(fetched, default=None)  # type: ignore[type-var]


def screen_stocks(today: date) -> tuple[list[Pick], object]:
    """Every company passing the tests, best first, and the data's age."""
    companies, series, fetched_at = _load()
    picks = []
    for key, company in companies.items():
        passed = _passes(company.ratios, series.get(key, {}), today)
        if passed is not None:
            roe, pe, pe_sector, revenue_growth, profit_growth, latest = passed
            picks.append(
                Pick(
                    company.ticker,
                    company.name,
                    roe,
                    pe,
                    pe_sector,
                    revenue_growth,
                    profit_growth,
                    period_label(latest),
                )
            )
    return _rank(picks), fetched_at


def _describe(position: int, pick: Pick) -> str:
    pe = f"P/E {pick.pe.normalize():f}"
    if pick.pe_sector:
        pe += f" vs sector {pick.pe_sector.normalize():f}"
    return (
        f"{position}. {pick.ticker} ({pick.name}): ROE {pick.roe.normalize():f}%, "
        f"profit +{pick.profit_growth}%/yr and revenue +{pick.revenue_growth}%/yr "
        f"over {GROWTH_YEARS} years to {pick.latest_period}, {pe}"
    )


TESTS = (
    "Tests every company passed\n"
    f"  Established: yearly revenue of at least ₹{MIN_REVENUE_CRORE:,} crore\n"
    f"  Consistent: revenue and net profit rose in each of the last "
    f"{GROWTH_YEARS} years, with no single-year jump over "
    f"{MAX_YEARLY_GROWTH_PCT}% (mergers and one-offs)\n"
    f"  Profitable: ROE of at least {MIN_ROE}% ({MIN_BANK_ROE}% for banks), "
    "and not below its sector\n"
    f"  Balance sheet: liabilities at most {MAX_LIABILITIES_TO_ASSETS:.0%} of "
    f"assets (banks: Net NPA at most {MAX_BANK_NET_NPA}%)\n"
    f"  Valuation: profitable P/E, at most {MAX_PE_TO_SECTOR}x its sector "
    f"(or {MAX_PE_WITHOUT_SECTOR} when the sector figure is unusable)"
)

CAVEATS = (
    "Before investing, check\n"
    "  - Past growth does not guarantee future returns, and none of this "
    "covers price trend, news, management or corporate actions.\n"
    "  - Spread money across companies and sectors rather than buying one "
    "list wholesale.\n"
    '  - Ask "Should I buy <SYMBOL>?" for any of these to see its live price '
    "and a full review."
)


def run(question: str) -> PathResult:
    """Render the screen; the question only chose this path."""
    result = PathResult()
    picks, fetched_at = screen_stocks(date.today())
    shown = picks[:SHOWN]
    result.sql_calls.append(
        SqlResult(
            sql=RATIOS_SQL + FINANCIALS_SQL,
            columns=["ticker", "roe", "profit_growth"],
            rows=[
                {
                    "ticker": p.ticker,
                    "roe": str(p.roe),
                    "profit_growth": str(p.profit_growth),
                }
                for p in shown
            ],
        )
    )
    intro = (
        "I can't tell you what to buy or promise profits. These companies "
        "passed the quality tests below on the latest stored data; treat the "
        "list as a starting point for your own research."
    )
    if not shown:
        result.text = (
            f"Long-term quality screen\n{intro}\n\n{TESTS}\n\n"
            "No company passes every test on the stored data. Run "
            "`sra upstox ingest-fundamentals --all` to refresh it.\n\n"
            f"{DISCLAIMER}"
        )
        return result
    as_of = f" (data as of {ist_time(fetched_at)})" if fetched_at else ""
    ranked = (
        f"Top {len(shown)} of {len(picks)} that pass, ranked by ROE, profit "
        f"growth and P/E against sector{as_of}\n"
        + "\n".join(f"  {_describe(i, p)}" for i, p in enumerate(shown, start=1))
    )
    result.text = "\n\n".join(
        [
            f"Long-term quality screen\n{intro}",
            TESTS,
            ranked,
            CAVEATS,
            DISCLAIMER,
        ]
    )
    return result
