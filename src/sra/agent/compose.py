"""Route a question and answer it through exactly one path.

Each route owns one path; a question that routes to general finance is first
checked against the instruments table, so a company named in plain words still
reaches the data paths. Code-rendered answers (holding calls among them,
whose call is scored in code and only worded by the model) cannot be talked
out of their figures; the other routes run a single tool-using model loop. Follow-ups are rewritten to stand
alone first, so no path ever sees earlier turns.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sra.agent import (
    calculators,
    compare,
    derivatives,
    discover,
    dividends,
    documents,
    explain,
    funds,
    history,
    market,
    news,
    portfolio,
    results,
    screen,
    tax,
    watchlist,
)
from sra.agent.companies import resolve_companies
from sra.agent.followup import Turn, standalone_question
from sra.agent.loop import (
    PathResult,
    fundamentals_path,
    general_finance_path,
    realtime_path,
)
from sra.agent.router import Route, Routing, route_question

OUT_OF_SCOPE = """\
I cover only NSE and BSE listed Indian equities. Ask about an Indian ticker or
a general finance concept."""


@dataclass
class Answer:
    question: str
    routing: Routing
    # What was actually routed and answered: the question itself, or a
    # follow-up rewritten to stand alone using the conversation.
    standalone: str = ""
    text: str = ""
    path: PathResult | None = None

    @property
    def route(self) -> Route:
        return self.routing.route

    @property
    def paths(self) -> list[PathResult]:
        return [] if self.path is None else [self.path]

    @property
    def stopped_early(self) -> bool:
        return self.path is not None and self.path.stopped_early

    @property
    def rounds(self) -> int:
        return 0 if self.path is None else self.path.rounds


def ask(
    question: str,
    *,
    history: list[Turn] | None = None,
    max_rounds: int | None = None,
) -> Answer:
    """Answer a question through whichever path or paths it needs.

    With history, a follow-up is first rewritten to stand alone; the paths
    then see only that question, never the earlier turns.
    """
    original = question
    question = standalone_question(question, history) if history else question
    routing = route_question(question)
    # Ordinary phrasing ("what is the pe of infosys") carries no uppercase
    # symbol, so the regexes alone send it to the tool-less path. One indexed
    # lookup tells whether the question names a listed company.
    if routing.route is Route.GENERAL_FINANCE:
        found = resolve_companies(question)
        if found.companies:
            routing = route_question(
                question, names_company=True, company_certain=found.confident
            )
    answer = Answer(question=original, routing=routing, standalone=question)
    kwargs = {} if max_rounds is None else {"max_rounds": max_rounds}

    if routing.route is Route.OUT_OF_SCOPE:
        answer.text = OUT_OF_SCOPE
        return answer

    code_path = CODE_PATHS.get(routing.route)
    if routing.route is Route.WATCHLIST:
        # The only answer that writes: it must see what the user typed, not
        # just the rewrite (see watchlist.answer).
        answer.path = watchlist.answer(question, typed=original)
    elif code_path is not None:
        answer.path = code_path(question)
    else:
        answer.path = _MODEL_PATHS[routing.route](question, **kwargs)
    answer.text = answer.path.text
    return answer


# Rendered in code, not by the model: figures, comparisons and lists of
# stocks must come from real data and stated rules, never from sampling.
CODE_PATHS: dict[Route, Callable[[str], PathResult]] = {
    # One holding gets the holding review, several the portfolio review.
    Route.ADVICE: portfolio.answer,
    Route.PORTFOLIO: portfolio.answer,
    Route.TAX: tax.answer,
    Route.DISCOVER: discover.answer,
    Route.NEWS: news.news,
    Route.ANNOUNCEMENTS: news.announcements,
    Route.DOCUMENTS: documents.answer,
    Route.EXPLAIN: explain.answer,
    Route.CALCULATOR: calculators.answer,
    Route.FUNDS: funds.answer,
    Route.OPTIONS: derivatives.options,
    Route.SHAREHOLDING: derivatives.shareholding,
    Route.NOT_COVERED: derivatives.not_covered,
    Route.WATCHLIST: watchlist.answer,
    Route.SCREEN: screen.run,
    Route.HISTORY: history.answer,
    Route.MARKET: market.answer,
    Route.COMPARE: compare.answer,
    Route.RESULTS: results.answer,
    Route.DIVIDENDS: dividends.answer,
}

_MODEL_PATHS = {
    Route.REALTIME: realtime_path,
    Route.FUNDAMENTALS: fundamentals_path,
    Route.GENERAL_FINANCE: general_finance_path,
}
