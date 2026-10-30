"""Stored fundamentals for one company or all of them, a query per table.

The screen, comparisons, results and peer lists all read companies through
here, so every one of them sees the same ratios, the same date-ordered
series and the same sector label.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg

from sra.db import connect
from sra.figures import period_start, to_decimal

Ratio = tuple[Decimal | None, Decimal | None]  # (company, sector)
Series = list[tuple[date, Decimal]]  # oldest first

_KEYS = "(%(keys)s::text[] IS NULL OR i.instrument_key = ANY(%(keys)s))"

RATIOS_SQL = f"""
SELECT i.instrument_key, i.ticker, i.name, fr.name AS metric,
       fr.company_value, fr.sector_value, fr.fetched_at
FROM fundamental_ratios fr
JOIN instruments i ON i.instrument_key = fr.instrument_key
WHERE i.exchange = 'NSE_EQ' AND {_KEYS}
"""

FINANCIALS_SQL = f"""
SELECT ff.instrument_key, ff.time_period, ff.category, ff.period, ff.value
FROM fundamental_financials ff
JOIN instruments i ON i.instrument_key = ff.instrument_key
WHERE i.exchange = 'NSE_EQ' AND {_KEYS}
  AND (ff.time_period = 'yearly' OR %(quarterly)s)
"""

PROFILES_SQL = f"""
SELECT p.instrument_key, p.sector
FROM company_profiles p
JOIN instruments i ON i.instrument_key = p.instrument_key
WHERE i.exchange = 'NSE_EQ' AND {_KEYS}
"""


@dataclass
class CompanyFacts:
    key: str
    ticker: str
    name: str
    ratios: dict[str, Ratio] = field(default_factory=dict)
    # category -> series; income statement, balance sheet and cash flow
    # categories do not overlap ('revenue', 'total_asset', 'operating', ...).
    yearly: dict[str, Series] = field(default_factory=dict)
    quarterly: dict[str, Series] = field(default_factory=dict)
    sector: str | None = None

    @property
    def is_bank(self) -> bool:
        return "NIM" in self.ratios

    def ratio(self, name: str) -> Decimal | None:
        return self.ratios.get(name, (None, None))[0]


@dataclass(frozen=True)
class Universe:
    companies: dict[str, CompanyFacts]
    fetched_at: datetime | None  # newest ratio fetch, for "data as of"


def load(keys: list[str] | None = None, *, quarterly: bool = False) -> Universe:
    """Facts for the given instrument keys, or for every NSE listing."""
    params: dict[str, Any] = {"keys": keys, "quarterly": quarterly}
    companies: dict[str, CompanyFacts] = {}
    fetched: list[datetime] = []
    with connect(readonly=True) as conn:
        for row in conn.execute(RATIOS_SQL, params).fetchall():
            key = str(row["instrument_key"])
            company = companies.setdefault(
                key, CompanyFacts(key, str(row["ticker"]), str(row["name"]))
            )
            company.ratios[str(row["metric"])] = (
                to_decimal(row["company_value"]),
                to_decimal(row["sector_value"]),
            )
            fetched.append(row["fetched_at"])
        for row in conn.execute(FINANCIALS_SQL, params).fetchall():
            owner = companies.get(str(row["instrument_key"]))
            when, value = period_start(str(row["period"])), to_decimal(row["value"])
            if owner is None or when is None or value is None:
                continue
            by_category = (
                owner.yearly if row["time_period"] == "yearly" else owner.quarterly
            )
            by_category.setdefault(str(row["category"]), []).append((when, value))
        try:
            # company_profiles arrives with migration 009; until it is applied
            # (or ingested) companies simply have no sector.
            with conn.transaction():
                for row in conn.execute(PROFILES_SQL, params).fetchall():
                    profiled = companies.get(str(row["instrument_key"]))
                    if profiled is not None and row["sector"]:
                        profiled.sector = str(row["sector"])
        except psycopg.errors.UndefinedTable:
            pass
    for company in companies.values():
        for series in (*company.yearly.values(), *company.quarterly.values()):
            series.sort()
    return Universe(companies, max(fetched, default=None))
