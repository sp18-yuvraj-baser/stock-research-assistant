"""Integrity of the eval set itself.

An eval set with a mislabelled or unusable question quietly reports a fault in
the system that is really a fault in the question.
"""

import json
from pathlib import Path

import pytest

from sra.eval.spec import (
    ADVICE,
    HYBRID,
    NARRATIVE,
    NUMERIC,
    QUESTIONS_PATH,
    REFUSAL,
    EvalQuestion,
    FactSpec,
    load_questions,
)

QUESTIONS = load_questions()
KINDS = {NUMERIC, NARRATIVE, HYBRID, REFUSAL, ADVICE}


def test_every_kind_is_represented() -> None:
    assert {q.kind for q in QUESTIONS} == KINDS


def test_ids_are_unique() -> None:
    ids = [q.id for q in QUESTIONS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("question", QUESTIONS, ids=lambda q: q.id)
def test_question_is_usable(question: EvalQuestion) -> None:
    assert question.kind in KINDS
    assert question.question.strip().endswith(("?", ".")), "must read as a question"

    if question.kind in (NUMERIC, HYBRID):
        assert question.expected_facts, "needs a fact spec to score against"
    if question.kind in (NARRATIVE, HYBRID):
        assert question.expected_sections, "needs the sections that should answer it"
    if question.kind in (REFUSAL, ADVICE):
        assert not question.expected_facts and not question.expected_sections
    if question.kind == REFUSAL:
        assert question.notes, "a refusal needs to say why the data is absent"


@pytest.mark.parametrize(
    "spec",
    [s for q in QUESTIONS for s in q.expected_facts],
    ids=lambda s: s.describe().replace(" ", "_"),
)
def test_fact_specs_are_well_formed(spec: FactSpec) -> None:
    assert spec.period_type in {"instant", "quarter", "ytd", "annual", "other"}
    # A spec must pin a period somehow, or it could match any row.
    assert spec.latest or spec.fiscal_year is not None


def test_the_set_covers_every_ingested_filer() -> None:
    raw = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    blob = json.dumps(raw)
    for ticker in ("Nvidia", "Apple", "Microsoft", "Costco", "Walmart"):
        assert ticker in blob, f"no question mentions {ticker}"


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dupes.json"
    path.write_text(
        json.dumps(
            [
                {"id": "q1", "question": "A?", "type": NARRATIVE},
                {"id": "q1", "question": "B?", "type": NARRATIVE},
            ]
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_questions(path)
