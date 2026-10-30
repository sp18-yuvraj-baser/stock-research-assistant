"""Answer mutual-fund questions: NAV, returns, fund vs fund, holdings.

NAVs come from AMFI (stored by `sra ingest funds`), history from
api.mfapi.in, and returns are computed here. Portfolio holdings need the
optional BharatStock key. Direct-plan Growth options are shown unless the
question asks for the regular plan.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

import httpx
import psycopg

from sra import bharatstock
from sra.agent.loop import PathResult
from sra.db import connect
from sra.feeds import USER_AGENT

MFAPI_URL = "https://api.mfapi.in/mf/{code}"
MAX_FUNDS = 4

_STOP = frozenset(
    """
    mutual fund funds mf nav navs return returns performance performed holdings
    holding portfolio portfolios of the a an what is are was direct regular plan
    growth option scheme schemes compare vs versus and or with latest current
    today year years yr month months show me tell how did has which in for its
    stocks held top best between price value
    """.split()  # noqa: SIM905 -- a word list reads better as prose
)

FIND_SQL = """
SELECT scheme_code, name, fund_house, category, plan, option, nav, nav_date
FROM funds
WHERE lower(name) LIKE ALL(%(patterns)s)
ORDER BY (plan ILIKE %(plan)s) DESC, (option ILIKE 'growth%%') DESC,
         length(name), scheme_code
LIMIT 40
"""

NO_FUNDS = (
    "No mutual-fund NAVs are stored yet. Run `sra migrate`, then "
    "`sra ingest funds` (daily, e.g. from cron) to load AMFI's NAV file."
)


@dataclass(frozen=True)
class Fund:
    code: str
    name: str
    fund_house: str | None
    category: str | None
    plan: str | None
    option: str | None
    nav: Decimal | None
    nav_date: date | None


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9&]+", text.lower()) if w not in _STOP]


def find_fund(text: str, *, regular: bool) -> tuple[Fund | None, list[str]]:
    """The fund a phrase names (preferred plan first), or candidate names."""
    words = _words(text)
    if not words:
        return None, []
    with connect(readonly=True) as conn:
        rows = conn.execute(
            FIND_SQL,
            {
                "patterns": [f"%{w}%" for w in words],
                "plan": "regular%" if regular else "direct%",
            },
        ).fetchall()
    if not rows:
        return None, []
    names = list(dict.fromkeys(str(r["name"]) for r in rows))
    if len(names) > 1:
        # The closest name has the fewest words beyond the ones asked for:
        # "axis large cap" is Axis Large Cap Fund, not Axis Large & Mid Cap.
        def extra(name: str) -> int:
            return len(set(_words(name)) - set(words))

        fewest = min(extra(n) for n in names)
        closest = [n for n in names if extra(n) == fewest]
        if len(closest) != 1:
            return None, closest[:5]
        rows = [r for r in rows if r["name"] == closest[0]]
    row = rows[0]
    return (
        Fund(
            str(row["scheme_code"]),
            str(row["name"]),
            row["fund_house"],
            row["category"],
            row["plan"],
            row["option"],
            row["nav"],
            row["nav_date"],
        ),
        [],
    )


@lru_cache(maxsize=64)
def _history(code: str, day: date) -> tuple[tuple[date, float], ...]:
    # The day is part of the cache key, so a new day fetches afresh.
    response = httpx.get(
        MFAPI_URL.format(code=code), headers={"User-Agent": USER_AGENT}, timeout=20.0
    )
    response.raise_for_status()
    points = []
    for item in response.json().get("data") or []:
        try:
            when = datetime.strptime(str(item["date"]), "%d-%m-%Y").date()
            points.append((when, float(item["nav"])))
        except (KeyError, ValueError, TypeError):
            continue
    return tuple(sorted(points))


def fund_returns(code: str, today: date) -> list[tuple[str, float | None]]:
    """1, 3 and 5-year returns (compound a year beyond one) from NAV history."""
    history = _history(code, today)
    if not history:
        return []
    latest_day, latest = history[-1]
    out: list[tuple[str, float | None]] = []
    for label, years in (("1 year", 1), ("3 years", 3), ("5 years", 5)):
        target = latest_day - timedelta(days=365 * years)
        earlier = [nav for day, nav in history if day <= target]
        if not earlier or history[0][0] > target or earlier[-1] <= 0:
            out.append((label, None))
            continue
        out.append((label, 100 * ((latest / earlier[-1]) ** (1 / years) - 1)))
    return out


def _describe(fund: Fund, today: date) -> str:
    nav = f"₹{fund.nav:,.4f}".rstrip("0").rstrip(".") if fund.nav else "n/a"
    lines = [
        f"  NAV: {nav}" + (f" on {fund.nav_date:%d %b %Y}" if fund.nav_date else ""),
        f"  Category: {fund.category or 'n/a'}",
        f"  Fund house: {fund.fund_house or 'n/a'}",
        f"  Plan: {fund.plan or 'n/a'}, {fund.option or 'n/a'}",
    ]
    try:
        returns = fund_returns(fund.code, today)
    except (httpx.HTTPError, ValueError):
        returns = []
    if returns:
        for label, value in returns:
            shown = "not enough history" if value is None else f"{value:+.2f}%"
            suffix = " a year" if value is not None and label != "1 year" else ""
            lines.append(f"  {label}: {shown}{suffix}")
    else:
        lines.append("  Returns: NAV history unavailable right now")
    return f"{fund.name}\n" + "\n".join(lines)


def _holdings(fund: Fund) -> str:
    """Top holdings via BharatStock; field names are read defensively."""
    try:
        found = bharatstock.get(
            "/mf/portfolios", {"q": fund.name}, max_age=timedelta(days=1)
        )
        portfolios = found.get("data") if isinstance(found, dict) else found
        if not portfolios:
            return "Holdings\n  BharatStock has no portfolio for this fund."
        first = portfolios[0]
        scheme = str(first.get("scheme_name") or first.get("name") or fund.name)
        payload = bharatstock.get(
            f"/mf/portfolios/{bharatstock.path_part(scheme)}/holdings",
            {"page_size": "10"},
            max_age=timedelta(days=7),
        )
    except bharatstock.BharatStockError as exc:
        return f"Holdings\n  {exc}"
    items: Any = payload.get("data") if isinstance(payload, dict) else payload
    lines = []
    for item in (items or [])[:10]:
        if not isinstance(item, dict):
            continue
        name = next(
            (str(v) for k, v in item.items() if isinstance(v, str) and "name" in k), "?"
        )
        weight = next(
            (
                v
                for k, v in item.items()
                if any(t in k for t in ("pct", "percent", "weight"))
            ),
            None,
        )
        lines.append(f"  {name}" + (f": {weight}%" if weight is not None else ""))
    if not lines:
        return "Holdings\n  BharatStock returned no holdings."
    return f"Top holdings (BharatStock, {scheme})\n" + "\n".join(lines)


def answer(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    regular = bool(re.search(r"\bregular\b", question, re.IGNORECASE))
    parts = [
        p
        for p in re.split(r"\b(?:vs\.?|versus|and|compared? (?:to|with))\b|,", question,
                          flags=re.IGNORECASE)
        if p and _words(p)
    ][:MAX_FUNDS]  # fmt: skip
    try:
        found = [find_fund(part, regular=regular) for part in parts or [question]]
    except psycopg.errors.UndefinedTable:
        return PathResult(text=NO_FUNDS)
    funds = [fund for fund, _ in found if fund is not None]
    unclear = [names for fund, names in found if fund is None and names]
    if not funds:
        options = next((names for _, names in found if names), [])
        if options:
            return PathResult(
                text="Which fund do you mean?\n"
                + "\n".join(f"  - {name}" for name in options)
            )
        return PathResult(text="No stored fund matches that name. " + NO_FUNDS)

    sections = [
        ("Fund comparison" if len(funds) > 1 else "Mutual fund")
        + "\nAMFI NAV; returns computed from api.mfapi.in NAV history (growth "
        "option, before tax and exit loads)."
    ]
    sections += [_describe(fund, today) for fund in funds]
    for names in unclear:
        sections.append(
            "Also asked about, but the name matches several funds\n"
            + "\n".join(f"  - {name}" for name in names)
        )
    if re.search(r"\b(holdings?|portfolio|stocks held)\b", question, re.IGNORECASE):
        sections.append(_holdings(funds[0]))
    sections.append(
        "Not covered here: expense ratio, exit load, fund manager. Past "
        "returns do not predict future returns."
    )
    return PathResult(text="\n\n".join(sections))
