"""Run the eval set and score it.

Running is slow because every question drives a local model; scoring is
instant. The two are separate commands so a scoreboard can be reprinted, and
failures re-examined, without paying for another run.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sra.agent.compose import ask
from sra.agent.router import Route
from sra.config import PROJECT_ROOT, settings
from sra.eval.checks import (
    EXACT,
    QUOTED,
    ROUNDED,
    UNTRACEABLE,
    cited_accessions,
    classify_figures,
    has_unbalanced_quotes,
    looks_like_refusal,
    quoted_spans,
    sections_hit,
    value_match,
)
from sra.eval.spec import (
    ADVICE,
    HYBRID,
    NARRATIVE,
    NUMERIC,
    REFUSAL,
    EvalQuestion,
    distractor_values,
    load_questions,
    resolve_fact,
)

RESULTS_PATH = PROJECT_ROOT / "evals" / "results.json"

# Enough to cut wall-clock time without thrashing a 32k-context model.
DEFAULT_WORKERS = 3


@dataclass
class ExpectedFactOutcome:
    spec: str
    expected_value: str
    found: bool
    found_rounded: bool
    distractors_present: list[str] = field(default_factory=list)


@dataclass
class QuestionResult:
    id: str
    question: str
    kind: str
    route: str
    answer: str
    latency_seconds: float
    stopped_early: bool
    # Raw evidence, so a fix to a check re-scores stored results instead of
    # needing another run. Storing only the verdicts made the split pointless.
    sql_values: list[str] = field(default_factory=list)
    passages: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    expected_facts: list[dict[str, Any]] = field(default_factory=list)
    expected_sections: list[str] = field(default_factory=list)
    retrieved_sections: list[str] = field(default_factory=list)
    sections_matched: list[str] = field(default_factory=list)
    cited_accessions: list[str] = field(default_factory=list)
    evidence_accessions: list[str] = field(default_factory=list)
    quoted_spans_total: int = 0
    quoted_spans_verified: int = 0
    refusal_detected: bool = False
    unbalanced_quotes: bool = False
    unresolved_specs: list[str] = field(default_factory=list)


def _evaluate(question: EvalQuestion) -> QuestionResult:
    """Answer and score one question, recording a crash rather than raising.

    A full run takes minutes; losing all of it to one bad question would make
    the harness useless exactly when something is broken.
    """
    try:
        return _evaluate_once(question)
    except Exception as exc:
        return QuestionResult(
            id=question.id,
            question=question.question,
            kind=question.kind,
            route="error",
            answer=f"ERROR: {type(exc).__name__}: {exc}",
            latency_seconds=0.0,
            stopped_early=True,
        )


def _evaluate_once(question: EvalQuestion) -> QuestionResult:
    started = time.monotonic()
    answer = ask(question.question)
    latency = time.monotonic() - started

    sql_values: set[Decimal] = set()
    for path in answer.paths:
        for value in path.cited_values:
            try:
                sql_values.add(Decimal(value))
            except (ArithmeticError, ValueError, TypeError):
                # Non-numeric columns (tags, dates, accessions) come back too.
                continue

    passages = [
        passage.text
        for path in answer.paths
        for call in path.search_calls
        for passage in call.passages
    ]
    retrieved_sections = sorted(
        {
            passage.section
            for path in answer.paths
            for call in path.search_calls
            for passage in call.passages
        }
    )
    evidence = sorted({acc for path in answer.paths for acc in path.accessions})

    verdicts = classify_figures(answer.text, sql_values, passages)
    spans = quoted_spans(answer.text)

    result = QuestionResult(
        id=question.id,
        question=question.question,
        kind=question.kind,
        route=answer.route.value,
        answer=answer.text,
        latency_seconds=round(latency, 2),
        stopped_early=answer.stopped_early,
        sql_values=sorted(str(v) for v in sql_values),
        passages=passages,
        # Kept so a wrong answer can be traced to the query that produced it.
        queries=[call.sql for path in answer.paths for call in path.sql_calls],
        figures=[
            {
                "text": v.figure.text,
                "value": str(v.figure.value),
                "status": v.status,
                "matched": v.matched,
            }
            for v in verdicts
        ],
        expected_sections=question.expected_sections,
        retrieved_sections=retrieved_sections,
        sections_matched=sections_hit(question.expected_sections, retrieved_sections),
        cited_accessions=sorted(cited_accessions(answer.text)),
        evidence_accessions=evidence,
        quoted_spans_total=len(spans),
        quoted_spans_verified=sum(
            1 for span in spans if any(span[:80] in p for p in passages)
        ),
        refusal_detected=looks_like_refusal(answer.text),
        unbalanced_quotes=has_unbalanced_quotes(answer.text),
    )

    if question.expected_facts:
        with psycopg.connect(settings().dsn, row_factory=dict_row) as conn:
            for spec in question.expected_facts:
                fact = resolve_fact(conn, spec)
                if fact is None:
                    result.unresolved_specs.append(spec.describe())
                    continue
                # Compare normalised values rather than provenance labels: a
                # figure can be present as an exact match, a rounded restatement
                # or inside a quotation, and any of those counts as present.
                matches = {value_match(v.figure.value, fact.value) for v in verdicts}
                distractors = distractor_values(conn, fact)
                present = [
                    label
                    for label, value in distractors.items()
                    if any(v.figure.value == value for v in verdicts)
                ]
                result.expected_facts.append(
                    asdict(
                        ExpectedFactOutcome(
                            spec=spec.describe(),
                            expected_value=str(fact.value),
                            found=EXACT in matches,
                            found_rounded=ROUNDED in matches,
                            distractors_present=present,
                        )
                    )
                )
    return result


def run_eval(
    *,
    only: list[str] | None = None,
    workers: int = DEFAULT_WORKERS,
    path: Path | None = None,
) -> list[QuestionResult]:
    """Answer every question and record what came back."""
    questions = load_questions()
    if only:
        wanted = {q.lower() for q in only}
        questions = [
            q for q in questions if q.id.lower() in wanted or q.kind.lower() in wanted
        ]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(_evaluate, questions))
    results.sort(key=lambda r: r.id)
    target = path or RESULTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([asdict(r) for r in results], indent=2) + "\n", encoding="utf-8"
    )
    return results


class StaleResultsError(RuntimeError):
    """Stored results predate the evidence fields scoring now needs."""


def load_results(path: Path | None = None) -> list[QuestionResult]:
    target = path or RESULTS_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    # Scoring re-derives figure verdicts from stored evidence. A file written
    # before that evidence was recorded would score every figure as
    # untraceable, which reads as a catastrophic result rather than as a stale
    # file, so refuse it outright.
    if payload and not any("sql_values" in row for row in payload):
        raise StaleResultsError(
            f"{target} was written before figure evidence was recorded, so it "
            "cannot be rescored. Re-run `sra eval run`."
        )
    known = {f.name for f in fields(QuestionResult)}
    return [
        QuestionResult(**{k: v for k, v in row.items() if k in known})
        for row in payload
    ]


@dataclass(frozen=True)
class Metric:
    name: str
    passed: int
    total: int
    catches: str

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _verdicts(result: QuestionResult) -> list[dict[str, Any]]:
    """Re-classify a stored answer's figures against its stored evidence."""
    values: set[Decimal] = set()
    for raw in result.sql_values:
        try:
            values.add(Decimal(raw))
        except (ArithmeticError, ValueError, TypeError):
            continue
    return [
        {
            "text": v.figure.text,
            "value": str(v.figure.value),
            "status": v.status,
            "matched": v.matched,
        }
        for v in classify_figures(result.answer, values, result.passages)
    ]


def score(results: list[QuestionResult]) -> tuple[list[Metric], list[str]]:
    """Compute the scoreboard and itemise every failure."""
    failures: list[str] = []
    metrics: list[Metric] = []

    # Figures are re-derived here rather than trusted from the run, so a
    # correction to the extractor takes effect without another run.
    for result in results:
        if result.sql_values or result.passages or result.answer:
            result.figures = _verdicts(result)

    # Headline: a figure in an answer that traces to no evidence at all.
    clean_answers = [
        r for r in results if not any(f["status"] == UNTRACEABLE for f in r.figures)
    ]
    metrics.append(
        Metric(
            "answers with no untraceable figure",
            len(clean_answers),
            len(results),
            "invented or mis-transcribed numbers",
        )
    )
    for r in results:
        for f in r.figures:
            if f["status"] == UNTRACEABLE:
                failures.append(
                    f"{r.id} untraceable figure {f['text']!r} "
                    f"(no run_sql result and not a verified quotation)"
                )

    numeric_like = [r for r in results if r.kind in (NUMERIC, HYBRID)]
    facts = [(r, f) for r in numeric_like for f in r.expected_facts]
    correct = [(r, f) for r, f in facts if f["found"] or f["found_rounded"]]
    metrics.append(
        Metric(
            "expected figure present",
            len(correct),
            len(facts),
            "right concept, wrong value",
        )
    )
    for r, f in facts:
        if not (f["found"] or f["found_rounded"]):
            failures.append(
                f"{r.id} missing expected {f['spec']} = {f['expected_value']}"
            )

    # A period error means the wrong period's value stood in for the right
    # one. An answer that states the right figure and also shows neighbouring
    # periods is giving context: "why did margin change last quarter" has to
    # quote the prior quarter to answer at all.
    substituted = [
        (r, f)
        for r, f in facts
        if not (f["found"] or f["found_rounded"]) and f["distractors_present"]
    ]
    metrics.append(
        Metric(
            "no wrong-period figure substituted",
            len(facts) - len(substituted),
            len(facts),
            "fiscal/calendar confusion, quarter vs year-to-date",
        )
    )
    for r, f in substituted:
        failures.append(
            f"{r.id} gave {f['distractors_present']} instead of "
            f"{f['spec']} = {f['expected_value']}"
        )

    retrieval = [r for r in results if r.expected_sections]
    recalled = [r for r in retrieval if r.sections_matched]
    metrics.append(
        Metric(
            "expected section retrieved",
            len(recalled),
            len(retrieval),
            "the answering passage was never retrieved",
        )
    )
    for r in retrieval:
        if not r.sections_matched:
            failures.append(
                f"{r.id} retrieved {r.retrieved_sections or 'nothing'}, "
                f"expected one of {r.expected_sections}"
            )

    cited = [r for r in results if r.cited_accessions]
    grounded = [
        r for r in cited if set(r.cited_accessions) <= set(r.evidence_accessions)
    ]
    metrics.append(
        Metric(
            "every cited filing was actually read",
            len(grounded),
            len(cited),
            "citations pointing at filings the tools never returned",
        )
    )
    for r in cited:
        stray = set(r.cited_accessions) - set(r.evidence_accessions)
        if stray:
            failures.append(f"{r.id} cited unread filing(s): {sorted(stray)}")

    quoting = [r for r in results if r.quoted_spans_total]
    faithful = [r for r in quoting if r.quoted_spans_verified == r.quoted_spans_total]
    metrics.append(
        Metric(
            "quotations found verbatim in a retrieved passage",
            len(faithful),
            len(quoting),
            "paraphrase presented as a quotation",
        )
    )
    for r in quoting:
        if r.quoted_spans_verified != r.quoted_spans_total:
            failures.append(
                f"{r.id} {r.quoted_spans_total - r.quoted_spans_verified} of "
                f"{r.quoted_spans_total} quotation(s) not found in any passage"
            )

    balanced = [r for r in results if not r.unbalanced_quotes]
    metrics.append(
        Metric(
            "quotation marks balanced",
            len(balanced),
            len(results),
            "an unpaired quote leaves it unclear where a quotation ends",
        )
    )
    for r in results:
        if r.unbalanced_quotes:
            failures.append(f"{r.id} left an unpaired quotation mark")

    refusals = [r for r in results if r.kind == REFUSAL]
    declined = [r for r in refusals if r.refusal_detected]
    metrics.append(
        Metric(
            "declined when the filings do not cover it",
            len(declined),
            len(refusals),
            "answering from memory instead of saying it is not there",
        )
    )
    for r in refusals:
        if not r.refusal_detected:
            failures.append(f"{r.id} did not decline: {r.answer[:110]!r}")

    advice = [r for r in results if r.kind == ADVICE]
    refused = [r for r in advice if r.route == Route.ADVICE.value]
    metrics.append(
        Metric(
            "advice declined",
            len(refused),
            len(advice),
            "offering a recommendation; must be every time",
        )
    )
    for r in advice:
        if r.route != Route.ADVICE.value:
            failures.append(f"{r.id} routed to {r.route}, not declined as advice")

    routed = [
        r
        for r in results
        if r.kind in (NUMERIC, NARRATIVE, HYBRID) and r.route == r.kind
    ]
    routable = [r for r in results if r.kind in (NUMERIC, NARRATIVE, HYBRID)]
    metrics.append(
        Metric("routed as labelled", len(routed), len(routable), "router misroutes")
    )
    for r in routable:
        if r.route != r.kind:
            failures.append(f"{r.id} routed {r.route}, labelled {r.kind}")

    finished = [r for r in results if not r.stopped_early]
    metrics.append(
        Metric(
            "finished within the round limit",
            len(finished),
            len(results),
            "loops that ran out of tool rounds",
        )
    )
    for r in results:
        if r.stopped_early:
            failures.append(f"{r.id} hit the tool round limit")

    return metrics, failures


def format_scoreboard(results: list[QuestionResult]) -> str:
    metrics, failures = score(results)
    figures = [f for r in results if r.figures for f in r.figures]
    buckets = {
        status: sum(1 for f in figures if f["status"] == status)
        for status in (EXACT, QUOTED, ROUNDED, UNTRACEABLE)
    }
    latencies = [r.latency_seconds for r in results]

    lines = [f"{len(results)} questions", ""]
    width = max(len(m.name) for m in metrics)
    for metric in metrics:
        bar = f"{metric.passed}/{metric.total}"
        pct = f"{metric.rate * 100:5.1f}%" if metric.total else "    n/a"
        lines.append(f"  {metric.name:<{width}}  {bar:>7}  {pct}")

    total_figures = len(figures) or 1
    lines += [
        "",
        f"HALLUCINATED-NUMBER RATE: "
        f"{buckets[UNTRACEABLE] / total_figures * 100:.1f}%"
        f"  ({buckets[UNTRACEABLE]} of {len(figures)} figures)",
        "",
        "  figure provenance",
        f"    exact match to a run_sql result   {buckets[EXACT]}",
        f"    verbatim quotation from a filing  {buckets[QUOTED]}",
        f"    restated at lower precision       {buckets[ROUNDED]}",
        f"    traceable to nothing              {buckets[UNTRACEABLE]}",
        "",
        f"  latency  p50 {_percentile(latencies, 0.5):.1f}s"
        f"  p95 {_percentile(latencies, 0.95):.1f}s"
        f"  max {max(latencies, default=0):.1f}s",
    ]

    if failures:
        lines += ["", f"failures ({len(failures)})"]
        lines += [f"  - {f}" for f in failures]
    else:
        lines += ["", "no failures"]
    return "\n".join(lines)
