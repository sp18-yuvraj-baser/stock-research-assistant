"""Decide which path a question needs.

Rule-based rather than an LLM classifier. The rules are deterministic, so the
same question always routes the same way and a routing failure is reproducible
and fixable; an LLM classifier would have to be scored statistically and would
add a model call to every question's latency. The cost is coverage: a question
using vocabulary absent from these lists falls to the default, and every route
carries the features that produced it so misroutes are diagnosable.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class Route(StrEnum):
    NUMERIC = "numeric"
    NARRATIVE = "narrative"
    HYBRID = "hybrid"
    ADVICE = "advice"


@dataclass(frozen=True)
class Routing:
    route: Route
    reasons: tuple[str, ...]


# Asking for a recommendation must be caught every time, so these are matched
# before anything else and kept deliberately broad.
_ADVICE = (
    r"should i (buy|sell|hold|invest|own|avoid)",
    r"(is|are) (it|this|that|they|\w+) a (good|bad|smart|safe)"
    r" (buy|investment|stock|bet)",
    r"worth (buying|selling|holding|investing)",
    r"price target",
    # A company name sits between the verb and the noun in practice:
    # "Will Microsoft's stock go up next quarter?"
    r"(will|would|could)\b.{0,40}?\b(stock|shares?|share price|price)\b"
    r".{0,20}?\b(go|rise|fall|drop|climb|reach|hit|outperform|be worth)",
    r"what.{0,20}(stock|shares?) (should|to) (i|we) (buy|sell|pick)",
    # "Can you recommend a semiconductor stock to buy?" -- the qualifier sits
    # between the article and the noun.
    r"(recommend|advise)\b.{0,40}?\b(stock|shares?|buy|sell|investment|purchase)",
    r"(which|what) (stock|shares?|company).{0,30}(should|would) (i|we)",
    r"what should (i|we) (buy|sell|invest|own)",
    r"do you (think|believe).{0,30}(buy|sell|invest|outperform|go up|go down)",
    r"(bullish|bearish|undervalued|overvalued)",
    r"invest(ment)? advice",
    r"(good|bad) time to (buy|sell)",
)

# A causal question cannot be answered by figures alone: the explanation lives
# in the filing text, and the figures it explains live in XBRL.
_CAUSAL = (
    r"\bwhy\b",
    r"what (drove|caused|is driving|was driving|drives)",
    r"(reason|reasons|driver|drivers) (for|behind|of)",
    r"explain (the |this |that )?(change|decline|drop|increase|growth|move|shift)",
    r"attributable to",
    r"account(s|ed)? for the (change|decline|increase|growth)",
    r"how (do|does|did) .{0,30}(explain|attribute|describe) ",
)

# Concepts that resolve to an XBRL tag.
_NUMERIC_CONCEPT = (
    r"\brevenues?\b",
    r"\bnet sales\b",
    r"\bgross (profit|margin)\b",
    r"\boperating (income|margin|expenses?)\b",
    r"\bnet income\b",
    r"\bearnings\b",
    r"\beps\b",
    r"\bearnings per share\b",
    r"\bcost of (revenue|sales|goods)\b",
    r"\b(r&d|research and development)\b",
    r"\btotal (assets|liabilities|equity)\b",
    r"\bstockholders.? equity\b",
    r"\bcash (and cash equivalents|flow|position)\b",
    r"\binventor(y|ies)\b",
    r"\bshares outstanding\b",
    r"\bdividends?\b",
    r"\bmargins?\b",
    r"\bbalance sheet\b",
)

# Quantity phrasing without a named concept, e.g. "how much did it spend".
_QUANTITY = (
    r"how (much|many)\b",
    r"what (was|were|is|are) the (total|number|amount|figure)",
    r"\btrend\b",
    r"\bgrowth rate\b",
)

# Period framing, not a quantity ask. "Which risk factors changed year over
# year" is a narrative question; the phrase modifies whatever is being compared
# rather than implying a figure, so it only counts alongside a real concept.
_COMPARISON = (
    r"over the (last|past) \d+ (quarters?|years?)",
    r"year[- ]over[- ]year",
    r"\bcompared (to|with)\b",
    r"\bversus\b",
)

# An explicit request to quote what a filing states. "What does Apple's MD&A
# say about Services revenue" is asking for reported text, not a figure, even
# though "revenue" also matches a numeric concept below. This framing wins
# over a bare numeric-concept match; it does not override a genuine causal ask
# ("why does management say revenue grew"), which still needs both paths.
_EXPLICIT_QUOTE_FRAME = (
    r"what does .{0,40}say",
    r"how does .{0,40}(describe|characterize)",
    r"what (does|do) .{0,40}(disclose|state)",
)

# Topics that only filing text covers.
_NARRATIVE_TOPIC = (
    r"\brisks?\b",
    r"\brisk factors?\b",
    r"management (say|says|said|discuss|discusses|describe|describes|note|notes)",
    r"what does .{0,30}say",
    r"\bdisclos(e|es|ed|ure|ures)\b",
    r"\bcite[sd]?\b",
    r"\bdescribe[sd]?\b",
    r"\bmention(s|ed)?\b",
    r"\bstrateg(y|ies)\b",
    r"\bcompetiti(on|ve|tors?)\b",
    r"\blitigation\b",
    r"\blegal proceedings?\b",
    r"\bcybersecurity\b",
    r"\bexport controls?\b",
    r"\bregulat(ion|ions|ory)\b",
    r"\bsupply chain\b",
    r"\bmd&a\b",
    r"\bguidance\b",
    r"\boutlook\b",
    r"\bconcerns?\b",
    r"\buncertaint(y|ies)\b",
)


def _matches(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


# How far into the question a quote-frame match may start and still count as
# framing the *whole* question. "What does Apple's MD&A say about revenue" has
# it at position 0; "How did margin change and what does management say" has
# it well past this, joined to an independent numeric clause by "and" -- that
# is a genuine hybrid, not a quote request that happens to mention a number.
_QUOTE_FRAME_LEAD_CHARS = 8


def _leads_the_question(text: str, patterns: tuple[str, ...]) -> bool:
    return any(
        (m := re.search(p, text, re.IGNORECASE))
        and m.start() <= _QUOTE_FRAME_LEAD_CHARS
        for p in patterns
    )


def route_question(question: str) -> Routing:
    """Classify a question, reporting the features that decided it."""
    text = question.strip()
    if not text:
        return Routing(route=Route.NARRATIVE, reasons=("empty question",))

    if advice := _matches(text, _ADVICE):
        return Routing(route=Route.ADVICE, reasons=tuple(f"advice:{p}" for p in advice))

    causal = _matches(text, _CAUSAL)
    concept = _matches(text, _NUMERIC_CONCEPT)
    quantity = _matches(text, _QUANTITY)
    comparison = _matches(text, _COMPARISON)
    topic = _matches(text, _NARRATIVE_TOPIC)
    quote_frame = _matches(text, _EXPLICIT_QUOTE_FRAME)
    numeric = concept + quantity
    if numeric:
        numeric = numeric + comparison

    # "What does X say about revenue" wants the stated text, not the figure --
    # a numeric concept incidentally named inside the topic does not make this
    # a numbers question. This only applies when the quote-frame IS the
    # question: "how did margin change and what does management say" joins an
    # independent numeric clause to the frame with "and", and stays hybrid.
    if quote_frame and not causal and _leads_the_question(text, _EXPLICIT_QUOTE_FRAME):
        numeric = []

    reasons = tuple(
        [f"causal:{p}" for p in causal]
        + [f"numeric:{p}" for p in numeric]
        + [f"topic:{p}" for p in topic]
        + [f"quote_frame:{p}" for p in quote_frame]
    )

    if numeric and (causal or topic):
        # Needs the figures and the stated explanation; neither alone answers it.
        return Routing(route=Route.HYBRID, reasons=reasons)
    if numeric:
        return Routing(route=Route.NUMERIC, reasons=reasons)
    if topic or causal:
        return Routing(route=Route.NARRATIVE, reasons=reasons)

    # Nothing matched. Filing text covers far more ground than the XBRL tags do,
    # so an unrecognised question is likelier to be answerable from prose.
    return Routing(route=Route.NARRATIVE, reasons=("no features matched",))
