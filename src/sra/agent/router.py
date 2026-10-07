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
    REALTIME = "realtime"
    FUNDAMENTALS = "fundamentals"


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


# Live-price phrasing for the NSE/BSE realtime-quote path. Distinct from
# _ADVICE's "price target"/"good buy" framing: this asks what a stock IS
# trading at right now, not what it SHOULD be worth or whether to act on it.
_REALTIME_PRICE = (
    r"trading at",
    r"\bcurrent price\b",
    r"\blive price\b",
    r"\bltp\b",  # NSE/Upstox jargon for last traded price
    r"what('s| is) .{0,30}(price|quote) (right now|currently|today)",
    r"\bprice right now\b",
)

# Required alongside _REALTIME_PRICE: without an explicit signal that the
# question is about the NSE/BSE universe, "what's it trading at right now"
# about Nvidia would misroute here, where there is no NVDA coverage at all.
# Ticker symbol is the primary signal for all 50; a handful of well-known
# companies also get a common-name alias. Best-effort, not exhaustive --
# same coverage tradeoff as every other pattern family in this module. Kept
# in sync with ingest.instruments_run.DEFAULT_NSE_TICKERS.
_INDIAN_INSTRUMENT = (
    r"\breliance\b",
    r"\bril\b",
    r"\btcs\b",
    r"\btata consultancy\b",
    r"\bhdfc ?bank\b",
    r"\bicici ?bank\b",
    r"\binfosys\b",
    r"\binfy\b",
    r"\bhindunilvr\b",
    r"\bhindustan unilever\b",
    r"\bitc\b",
    r"\bsbin\b",
    r"\bstate bank of india\b",
    r"\bbharti ?airtel\b",
    r"\bbajfinance\b",
    r"\bbajaj finance\b",
    r"\bkotak ?(mahindra)? ?bank\b",
    r"\blt\b",
    r"\blarsen ?(and|&) ?toubro\b",
    r"\bhcltech\b",
    r"\bhcl technologies\b",
    r"\baxis ?bank\b",
    r"\bmaruti\b",
    r"\bsun pharma\b",
    r"\basian ?paints\b",
    r"\btitan\b",
    r"\bultratech\b",
    r"\bwipro\b",
    r"\bnestle ?india\b",
    r"\badani ?enterprises\b|\badanient\b",
    r"\badani ?ports\b",
    r"\btata motors\b|\btmpv\b|\btmcv\b",
    r"\btata steel\b",
    r"\bpower ?grid\b",
    r"\bntpc\b",
    r"\bm&m\b|\bmahindra ?(and|&) ?mahindra\b",
    r"\bbajaj finserv\b",
    r"\bjsw steel\b",
    r"\btech mahindra\b",
    r"\bhdfc life\b",
    r"\bsbi life\b",
    r"\bgrasim\b",
    r"\bcipla\b",
    r"\bdr\.? ?reddy'?s?\b",
    r"\bbritannia\b",
    r"\beicher motors\b",
    r"\bcoal india\b",
    r"\bdivi'?s? ?lab\b",
    r"\bapollo hospitals?\b",
    r"\bhero motocorp\b",
    r"\bbpcl\b",
    r"\bongc\b",
    r"\bshriram finance\b",
    r"\bindusind ?bank\b",
    r"\btata consumer\b",
    r"\bupl\b",
    r"\bhindalco\b",
    r"\bbajaj ?auto\b",
    r"\btrent\b",
    r"\bjio financial\b|\bjiofin\b",
    r"\bnse\b",
    r"\bbse\b",
)

# Fundamentals concepts: ratios, financial-statement categories, and
# screening phrasing. Checked alongside _INDIAN_INSTRUMENT or _NIFTY_SCOPE --
# a screening question often names no specific company at all ("which Nifty
# 50 stocks have ROE > 20%"), so the scope gate is wider than REALTIME's.
_FUNDAMENTALS_CONCEPT = (
    r"\bp/?e\b",
    r"price.to.earnings",
    r"\broe\b",
    r"\broce\b",
    r"\broa\b",
    r"ev/ebitda",
    r"\bp/?b\b",
    r"price.to.book",
    r"\bfundamentals?\b",
    r"\bfundamentally\b",
    # "Compare TCS vs Infosys" names no ratio by itself, but two tracked
    # tickers plus "compare" has nothing else it could mean here -- there is
    # no Indian filing-text corpus to compare instead.
    r"\bcompare\b",
    r"\bvs\.?\b",
    r"revenue growth",
    r"quarterly results?",
    r"financial results?",
    r"net profit",
    r"operating profit",
    r"balance sheet",
    r"income statement",
)

_SCREENING = (
    r"\bwhich stocks\b",
    r"\bfind .{0,20}stocks\b",
    r"\bscreen\b",
    r"\bstocks with\b",
)

_NIFTY_SCOPE = (
    r"\bnifty\s?50\b",
    r"\bnifty\b",
)


def _matches(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


def mentions_tracked_instrument(question: str) -> bool:
    """Whether a question names one of the NSE tickers this tool tracks.

    Used outside routing too: an ADVICE question about TCS still needs the
    decline, but the alternatives it offers should be the live-quote path,
    not SEC-filing offers that do not exist for an Indian equity.
    """
    return bool(_matches(question, _INDIAN_INSTRUMENT))


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

    realtime_price = _matches(text, _REALTIME_PRICE)
    realtime_instrument = _matches(text, _INDIAN_INSTRUMENT)
    if realtime_price and realtime_instrument:
        reasons = tuple(f"realtime:{p}" for p in realtime_price) + tuple(
            f"instrument:{p}" for p in realtime_instrument
        )
        return Routing(route=Route.REALTIME, reasons=reasons)

    fundamentals_concept = _matches(text, _FUNDAMENTALS_CONCEPT) or _matches(
        text, _SCREENING
    )
    fundamentals_scope = realtime_instrument or _matches(text, _NIFTY_SCOPE)
    if fundamentals_concept and fundamentals_scope:
        reasons = tuple(f"fundamentals:{p}" for p in fundamentals_concept) + tuple(
            f"instrument:{p}" for p in fundamentals_scope
        )
        return Routing(route=Route.FUNDAMENTALS, reasons=reasons)

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
