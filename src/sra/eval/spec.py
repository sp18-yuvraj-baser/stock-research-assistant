"""The eval set and its self-building answer key.

Numeric questions store a specification of the fact they expect -- filer, tag,
fiscal period, period type -- and never a literal value. The expected figure is
resolved from the facts table at eval time, so the key cannot drift out of date
and a restatement changes the key along with the answer instead of turning a
correct answer into a failure.
"""

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from sra.config import PROJECT_ROOT

QUESTIONS_PATH = PROJECT_ROOT / "evals" / "questions.json"

NUMERIC = "numeric"
NARRATIVE = "narrative"
HYBRID = "hybrid"
REFUSAL = "refusal"
ADVICE = "advice"


@dataclass(frozen=True)
class FactSpec:
    ticker: str
    tag: str
    period_type: str
    unit: str
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    latest: bool = False

    def describe(self) -> str:
        period = (
            f"{self.fiscal_period} {self.fiscal_year}"
            if self.fiscal_year is not None
            else "latest"
        )
        return f"{self.ticker} {self.tag} {period} ({self.period_type})"


@dataclass(frozen=True)
class ResolvedFact:
    spec: FactSpec
    value: Decimal
    accession_no: str
    fiscal_year: int | None
    fiscal_period: str | None
    period_end: date


@dataclass(frozen=True)
class EvalQuestion:
    id: str
    question: str
    kind: str
    expected_facts: list[FactSpec] = field(default_factory=list)
    expected_sections: list[str] = field(default_factory=list)
    notes: str = ""


def _fact_spec(raw: dict[str, Any]) -> FactSpec:
    return FactSpec(
        ticker=str(raw["ticker"]).upper(),
        tag=str(raw["tag"]),
        period_type=str(raw.get("period_type", "annual")),
        unit=str(raw.get("unit", "USD")),
        fiscal_year=raw.get("fiscal_year"),
        fiscal_period=raw.get("fiscal_period"),
        latest=bool(raw.get("latest", False)),
    )


def load_questions(path: Path | None = None) -> list[EvalQuestion]:
    payload = json.loads((path or QUESTIONS_PATH).read_text(encoding="utf-8"))
    questions = [
        EvalQuestion(
            id=str(raw["id"]),
            question=str(raw["question"]),
            kind=str(raw["type"]),
            expected_facts=[_fact_spec(f) for f in raw.get("expected_facts", [])],
            expected_sections=[str(s) for s in raw.get("expected_sections", [])],
            notes=str(raw.get("notes", "")),
        )
        for raw in payload
    ]
    ids = [q.id for q in questions]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate question ids: {sorted(duplicates)}")
    return questions


_RESOLVE = """
SELECT f.value, f.accession_no, f.fiscal_year, f.fiscal_period, f.period_end
FROM facts_current f
JOIN companies c ON c.cik = f.cik
WHERE c.ticker = %(ticker)s
  AND f.tag = %(tag)s
  AND f.unit = %(unit)s
  AND f.period_type = %(period_type)s
  AND (%(fiscal_year)s::int IS NULL OR f.fiscal_year = %(fiscal_year)s)
  AND (%(fiscal_period)s::text IS NULL OR f.fiscal_period = %(fiscal_period)s)
ORDER BY f.period_end DESC
LIMIT 1
"""

# Same concept, a different period. A wrong-period answer is a distinct failure
# from a wrong figure, and naming it needs the values it could have confused.
_DISTRACTORS = """
SELECT f.value, f.fiscal_year, f.fiscal_period, f.period_end, f.period_type
FROM facts_current f
JOIN companies c ON c.cik = f.cik
WHERE c.ticker = %(ticker)s
  AND f.tag = %(tag)s
  AND f.unit = %(unit)s
  AND f.period_end <> %(period_end)s
ORDER BY abs(f.period_end - %(period_end)s::date)
LIMIT 12
"""


def resolve_fact(
    conn: psycopg.Connection[dict[str, Any]], spec: FactSpec
) -> ResolvedFact | None:
    """Look the expected figure up in the facts table."""
    row = conn.execute(
        _RESOLVE,
        {
            "ticker": spec.ticker,
            "tag": spec.tag,
            "unit": spec.unit,
            "period_type": spec.period_type,
            "fiscal_year": spec.fiscal_year,
            "fiscal_period": spec.fiscal_period,
        },
    ).fetchone()
    if row is None:
        return None
    return ResolvedFact(
        spec=spec,
        value=Decimal(str(row["value"])),
        accession_no=str(row["accession_no"]),
        fiscal_year=row["fiscal_year"],
        fiscal_period=row["fiscal_period"],
        period_end=row["period_end"],
    )


def distractor_values(
    conn: psycopg.Connection[dict[str, Any]], fact: ResolvedFact
) -> dict[str, Decimal]:
    """Values of the same concept in neighbouring periods, keyed by label."""
    rows = conn.execute(
        _DISTRACTORS,
        {
            "ticker": fact.spec.ticker,
            "tag": fact.spec.tag,
            "unit": fact.spec.unit,
            "period_end": fact.period_end,
        },
    ).fetchall()
    out: dict[str, Decimal] = {}
    for row in rows:
        label = (
            f"{row['fiscal_period']} {row['fiscal_year']} "
            f"{row['period_type']} (ended {row['period_end']})"
        )
        out[label] = Decimal(str(row["value"]))
    return out
