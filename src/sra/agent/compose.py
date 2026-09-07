"""Route a question and compose the answer.

Composition happens here, in code, rather than by asking the model to merge the
two paths. A prompt instruction not to blend figures with prose is a request; a
renderer that receives two finished sub-answers and prints them under separate
headings with their own citations is a guarantee. It also keeps the failure
legible: if the numeric path found nothing, that block says so instead of the
narrative path quietly filling the gap.
"""

from dataclasses import dataclass, field

from sra.agent.loop import PathResult, narrative_path, numeric_path
from sra.agent.router import Route, Routing, route_question

FIGURES_HEADING = "Figures — from XBRL facts, queried directly"
NARRATIVE_HEADING = "What the filings say — from filing text"

# How many of the numeric path's periods to name when scoping the search.
PERIOD_HINT_LIMIT = 3

# Declining a recommendation is rendered here rather than left to the model, so
# it cannot be talked out of it and does not depend on sampling.
ADVICE_REFUSAL = """\
This is a research tool over SEC filings, not investment advice, so it will not
recommend buying, selling or holding anything, and it has no view on price.

What it can do instead:
  - show a figure over time, straight from XBRL facts, with the filing it came from
  - quote what management said about a trend in MD&A
  - list which risk factors a company discloses, and how they changed

Ask any of those about a company and period and it will answer with citations."""


@dataclass
class Answer:
    question: str
    routing: Routing
    text: str = ""
    numeric: PathResult | None = None
    narrative: PathResult | None = None
    paths: list[PathResult] = field(default_factory=list)

    @property
    def route(self) -> Route:
        return self.routing.route

    @property
    def cited_values(self) -> set[str]:
        """Figures that came from run_sql across every path that ran."""
        return {value for path in self.paths for value in path.cited_values}

    @property
    def cited_sections(self) -> set[tuple[str, str]]:
        return {section for path in self.paths for section in path.cited_sections}

    @property
    def stopped_early(self) -> bool:
        return any(path.stopped_early for path in self.paths)

    @property
    def rounds(self) -> int:
        return sum(path.rounds for path in self.paths)


def _period_hint(numeric: PathResult) -> str:
    """Tell the narrative path which periods the figures actually cover."""
    periods = numeric.periods[:PERIOD_HINT_LIMIT]
    if not periods:
        return ""
    return (
        "\n\nThe figures for this question come from these fiscal periods: "
        + "; ".join(periods)
        + ". Prefer filings covering those periods."
    )


def _alignment_note(numeric: PathResult, narrative: PathResult) -> str:
    """Flag a composed answer whose two halves discuss different filings.

    Both sub-answers can be individually correct while the composition
    misleads, if the explanation is drawn from a period the figures do not
    cover. Saying so is better than presenting them as one story.
    """
    numeric_filings = set(numeric.accessions)
    narrative_filings = {
        passage.accession_no
        for call in narrative.search_calls
        for passage in call.passages
    }
    if not numeric_filings or not narrative_filings:
        return ""
    if numeric_filings & narrative_filings:
        return ""
    return (
        "\nNote: the explanation above is drawn from filings other than the ones "
        "the figures came from, so it may describe a different period."
    )


def _block(heading: str, path: PathResult, empty_note: str) -> str:
    body = path.text.strip() or empty_note
    sources = ", ".join(path.accessions)
    footer = f"\nSources: {sources}" if sources else ""
    return f"{heading}\n{'-' * len(heading)}\n{body}{footer}"


def compose_hybrid(numeric: PathResult, narrative: PathResult) -> str:
    """Render both paths under separate headings, each with its own sources."""
    blocks = [
        _block(
            FIGURES_HEADING,
            numeric,
            "No figures for this were found in the XBRL facts.",
        ),
        _block(
            NARRATIVE_HEADING,
            narrative,
            "The filings retrieved do not appear to explain this.",
        )
        + _alignment_note(numeric, narrative),
    ]
    return "\n\n".join(blocks)


def ask(question: str, *, max_rounds: int | None = None) -> Answer:
    """Answer a question through whichever path or paths it needs."""
    routing = route_question(question)
    answer = Answer(question=question, routing=routing)
    kwargs = {} if max_rounds is None else {"max_rounds": max_rounds}

    if routing.route is Route.ADVICE:
        answer.text = ADVICE_REFUSAL
        return answer

    if routing.route in (Route.NUMERIC, Route.HYBRID):
        answer.numeric = numeric_path(question, **kwargs)
        answer.paths.append(answer.numeric)

    if routing.route in (Route.NARRATIVE, Route.HYBRID):
        # The numeric path runs first so its resolved periods can scope the
        # search; otherwise the two halves can discuss different quarters.
        hint = _period_hint(answer.numeric) if answer.numeric is not None else ""
        answer.narrative = narrative_path(f"{question}{hint}", **kwargs)
        answer.paths.append(answer.narrative)

    if routing.route is Route.HYBRID:
        assert answer.numeric is not None and answer.narrative is not None
        answer.text = compose_hybrid(answer.numeric, answer.narrative)
    elif answer.numeric is not None:
        answer.text = answer.numeric.text
    elif answer.narrative is not None:
        answer.text = answer.narrative.text

    return answer
