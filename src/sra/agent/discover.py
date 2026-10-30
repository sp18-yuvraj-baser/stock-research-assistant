"""Find companies: plain-language screens, sector lists and peers.

Rendered in code over every stored company (agent/universe.py). A screen
condition is parsed deterministically ("ROE above 20%", "P/E under 25",
"debt-free", "in IT"); anything not understood is said, never guessed.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.agent.universe import CompanyFacts, load, metrics

DISCLAIMER = (
    "This is research, not investment advice. For a personal recommendation, "
    "consult a SEBI-registered investment adviser."
)
SHOWN = 15
# Liabilities at most this share of assets counts as "debt-free"/"low debt";
# the data has no borrowings figure, so this is the closest honest proxy.
LOW_DEBT_PCT = Decimal(30)
# Below this yearly revenue, stored figures are often noise (an ROE of 500%
# on negative equity); screens leave such companies out unless the user sets
# a revenue condition of their own.
DEFAULT_MIN_REVENUE_CRORE = Decimal(500)
# Multiples that mean nothing at or below zero (a loss, negative book value).
_POSITIVE_ONLY = frozenset({"pe", "pb", "ev_ebitda"})

# name used in answers, metric key, words people use for it, unit suffix
_METRICS = (
    ("ROE", "roe", r"roe|return on equity", "%"),
    ("ROCE", "roce", r"roce|return on capital(?: employed)?", "%"),
    ("ROA", "roa", r"roa|return on assets", "%"),
    ("P/E", "pe", r"p/?e(?: ratio)?|pe ratio|price.to.earnings", ""),
    ("P/B", "pb", r"p/?b(?: ratio)?|price.to.book", ""),
    ("EV/EBITDA", "ev_ebitda", r"ev/?ebitda", ""),
    ("Revenue growth", "revenue_growth", r"(?:revenue|sales) growth", "%"),
    ("Profit growth", "profit_growth", r"(?:net )?profit growth|earnings growth", "%"),
    ("Net margin", "net_margin", r"(?:net |profit )?margins?", "%"),
    ("Revenue", "revenue", r"(?:revenue|sales)(?! growth)", " cr"),
    (
        "Liabilities/assets",
        "liabilities_to_assets",
        r"liabilities.to.assets|leverage",
        "%",
    ),
)
_ABOVE = r"(?:above|over|more than|greater than|at least|min(?:imum)?(?: of)?|>=?)"
_BELOW = r"(?:below|under|less than|at most|max(?:imum)?(?: of)?|<=?)"
_VALUE = r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent|cr|crore)?"

# Words people use for a sector, and the fragments of Upstox's sector labels
# they mean ("IT - Software", "Banks", ...).
SECTOR_WORDS = {
    "it": ("it -", "software", "information technology"),
    "tech": ("it -", "software", "information technology"),
    "bank": ("bank",),
    "banking": ("bank",),
    "pharma": ("pharma", "drug"),
    "healthcare": ("health", "hospital", "pharma"),
    "auto": ("auto",),
    "fmcg": ("fmcg", "consumer", "personal care", "food"),
    "cement": ("cement",),
    "steel": ("steel",),
    "metal": ("metal", "steel", "aluminium", "mining"),
    "power": ("power",),
    "energy": ("power", "oil", "gas", "energy", "refiner"),
    "oil": ("oil", "refiner"),
    "telecom": ("telecom",),
    "realty": ("realty", "real estate", "construction"),
    "insurance": ("insurance",),
    "chemical": ("chemical",),
    "defence": ("defence", "aerospace"),
    "finance": ("finance", "nbfc"),
    "textile": ("textile",),
    "infra": ("infra", "construction", "engineering"),
}
_SECTOR_WORD = r"\b(" + "|".join(sorted(SECTOR_WORDS, key=len, reverse=True)) + r")s?\b"


@dataclass(frozen=True)
class Condition:
    label: str
    key: str
    low: Decimal | None
    high: Decimal | None
    unit: str

    def holds(self, value: Decimal | None) -> bool:
        if value is None:
            return False
        if self.key in _POSITIVE_ONLY and value <= 0:
            return False
        if self.low is not None and value < self.low:
            return False
        return not (self.high is not None and value > self.high)

    def describe(self) -> str:
        if self.low is not None and self.high is not None:
            return f"{self.label} {self.low}-{self.high}{self.unit}"
        if self.low is not None:
            return f"{self.label} at least {self.low}{self.unit}"
        return f"{self.label} at most {self.high}{self.unit}"


def _number(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def parse_conditions(question: str) -> list[Condition]:
    text = question.lower()
    found: list[Condition] = []
    for label, key, words, unit in _METRICS:
        if any(c.key == key for c in found):
            continue
        between = re.search(
            rf"\b(?:{words})\b[^.,;]{{0,15}}?between {_VALUE} and {_VALUE}", text
        )
        if between:
            found.append(
                Condition(
                    label,
                    key,
                    _number(between.group(1)),
                    _number(between.group(3)),
                    unit,
                )
            )
            continue
        above = re.search(rf"\b(?:{words})\b[^.,;]{{0,15}}?{_ABOVE}\s*{_VALUE}", text)
        below = re.search(rf"\b(?:{words})\b[^.,;]{{0,15}}?{_BELOW}\s*{_VALUE}", text)
        if above or below:
            found.append(
                Condition(
                    label,
                    key,
                    _number(above.group(1)) if above else None,
                    _number(below.group(1)) if below else None,
                    unit,
                )
            )
    if re.search(r"\b(debt.?free|zero debt|no debt|low debt)\b", text) and not any(
        c.key == "liabilities_to_assets" for c in found
    ):
        found.append(
            Condition(
                "Liabilities/assets", "liabilities_to_assets", None, LOW_DEBT_PCT, "%"
            )
        )
    return found


def sector_fragments(question: str) -> tuple[str, tuple[str, ...]] | None:
    match = re.search(_SECTOR_WORD, question, re.IGNORECASE)
    if not match:
        return None
    word = match.group(1).lower()
    return word, SECTOR_WORDS[word]


def _in_sector(company: CompanyFacts, fragments: tuple[str, ...]) -> bool:
    sector = (company.sector or "").lower()
    return any(fragment in sector for fragment in fragments)


def _cell(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == " cr":
        return f"₹{value:,.0f} cr"
    return f"{value:.2f}".rstrip("0").rstrip(".") + unit


def _line(position: int, company: CompanyFacts, columns: list[Condition]) -> str:
    values = metrics(company)
    cells = ", ".join(f"{c.label} {_cell(values.get(c.key), c.unit)}" for c in columns)
    return f"  {position}. {company.ticker} ({company.name}): {cells}"


_DEFAULT_COLUMNS = [
    Condition("ROE", "roe", None, None, "%"),
    Condition("P/E", "pe", None, None, ""),
    Condition("Revenue", "revenue", None, None, " cr"),
]

HELP = (
    "Custom screen\n"
    'Give one or more conditions, e.g. "stocks with ROE above 20% and P/E below '
    '25", "debt-free IT companies", "revenue growth over 15% in pharma".\n\n'
    "Supported filters\n"
    + "\n".join(f"  - {label}" for label, *_ in _METRICS)
    + "\n  - Debt-free / low debt (liabilities at most 30% of assets)\n"
    "  - A sector word: IT, banks, pharma, auto, FMCG, metal, energy, ..."
)

NO_SECTORS = (
    "Sector labels are not stored yet. Run `sra migrate`, then "
    "`sra upstox ingest-fundamentals --all`, to fill them in."
)


def screen(question: str) -> PathResult:
    """Companies meeting every parsed condition, sorted by the first one."""
    conditions = parse_conditions(question)
    sector = sector_fragments(question)
    if not conditions and not sector:
        return PathResult(text=HELP)
    universe = list(load().companies.values())
    if sector and not any(c.sector for c in universe):
        return PathResult(text=NO_SECTORS)
    values = {c.key: metrics(c) for c in universe}
    floor = None
    if not any(cond.key == "revenue" for cond in conditions):
        floor = Condition("Revenue", "revenue", DEFAULT_MIN_REVENUE_CRORE, None, " cr")
    matches = [
        c
        for c in universe
        if all(cond.holds(values[c.key].get(cond.key)) for cond in conditions)
        and (floor is None or floor.holds(values[c.key].get("revenue")))
        and (sector is None or _in_sector(c, sector[1]))
    ]
    order = conditions[0] if conditions else _DEFAULT_COLUMNS[2]
    # A "below" condition ranks lowest first; anything else highest first.
    lowest_first = order.low is None and order.high is not None
    matches.sort(
        key=lambda c: values[c.key].get(order.key, Decimal(0)),
        reverse=not lowest_first,
    )
    columns = conditions or _DEFAULT_COLUMNS
    described = [c.describe() for c in conditions]
    if sector:
        described.append(f"sector: {sector[0]}")
    title = "Custom screen: " + "; ".join(described)
    if not matches:
        return PathResult(text=f"{title}\nNo stored company meets every condition.")
    rows = [_line(i, c, columns) for i, c in enumerate(matches[:SHOWN], start=1)]
    sections = [
        f"{title}\n{len(matches)} companies match"
        + (f"; the first {SHOWN} are shown." if len(matches) > SHOWN else "."),
        f"Sorted by {order.label}\n" + "\n".join(rows),
        "Notes\n"
        + (
            f"  - Only companies with yearly revenue of at least "
            f"₹{DEFAULT_MIN_REVENUE_CRORE:,} crore; add a revenue condition to "
            "change that.\n"
            if floor
            else ""
        )
        + "  - Growth is compounded over 3 years of yearly results.\n"
        "  - P/E, P/B and EV/EBITDA conditions skip loss-making companies.\n"
        "  - Debt-to-equity is not in the data; debt-free means liabilities at "
        "most 30% of assets.",
        DISCLAIMER,
    ]
    return PathResult(text="\n\n".join(sections))


def sector_list(question: str) -> PathResult:
    """The largest stored companies in a sector, by latest revenue."""
    sector = sector_fragments(question)
    if sector is None:
        return screen(question)
    universe = list(load().companies.values())
    if not any(c.sector for c in universe):
        return PathResult(text=NO_SECTORS)
    members = [c for c in universe if _in_sector(c, sector[1])]
    if not members:
        return PathResult(
            text=f"No stored company has a sector matching '{sector[0]}'."
        )
    members.sort(key=lambda c: metrics(c).get("revenue", Decimal(0)), reverse=True)
    labels = sorted({c.sector for c in members if c.sector})
    rows = [_line(i, c, _DEFAULT_COLUMNS) for i, c in enumerate(members[:SHOWN], 1)]
    return PathResult(
        text="\n\n".join(
            [
                f"{sector[0].upper() if len(sector[0]) <= 4 else sector[0].title()} "
                f"companies\n{len(members)} stored companies in "
                + ", ".join(labels)
                + "; largest by revenue first.",
                "Companies\n" + "\n".join(rows),
                "A list by size, not a recommendation; ask to compare any of them.",
            ]
        )
    )


def peers(question: str) -> PathResult:
    """Companies in the same sector as the one named, largest first."""
    found = resolve_companies(question)
    if len(found.companies) != 1:
        return PathResult(text='Name one company, e.g. "peers of Infosys".')
    target = found.companies[0]
    universe = load().companies
    facts = universe.get(target.instrument_key)
    if facts is None or not facts.sector:
        return PathResult(
            text=f"{target.name} ({target.ticker}) has no stored sector. " + NO_SECTORS
        )
    members = [c for c in universe.values() if c.sector == facts.sector]
    members.sort(key=lambda c: metrics(c).get("revenue", Decimal(0)), reverse=True)
    rows = [
        _line(i, c, _DEFAULT_COLUMNS)
        + (" (the company asked about)" if c is facts else "")
        for i, c in enumerate(members[:SHOWN], 1)
    ]
    return PathResult(
        text="\n\n".join(
            [
                f"{target.ticker} and its peers\nSector: {facts.sector}; "
                f"{len(members)} stored companies, largest by revenue first.",
                "Companies\n" + "\n".join(rows),
                "Peers here share Upstox's sector label; a business may compete "
                "with companies outside it.",
            ]
        )
    )


def answer(question: str) -> PathResult:
    if re.search(
        r"\b(peers?|competitors?|rivals?|compete|companies like)\b", question, re.I
    ):
        return peers(question)
    if parse_conditions(question):
        return screen(question)
    return sector_list(question)
