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
    numeric = concept + quantity
    if numeric:
        numeric = numeric + comparison

    reasons = tuple(
        [f"causal:{p}" for p in causal]
        + [f"numeric:{p}" for p in numeric]
        + [f"topic:{p}" for p in topic]
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
