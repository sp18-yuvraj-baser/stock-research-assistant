"""Deterministically route Indian-equity research questions.

Routing is a pure function of the text. Whether the text names a listed
company needs the instruments table, so the caller looks that up and passes
the answer in as `names_company` rather than this module touching the DB.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple


class Route(StrEnum):
    ADVICE = "advice"
    SCREEN = "screen"
    HISTORY = "history"
    MARKET = "market"
    COMPARE = "compare"
    RESULTS = "results"
    DIVIDENDS = "dividends"
    PORTFOLIO = "portfolio"
    TAX = "tax"
    DISCOVER = "discover"
    NEWS = "news"
    ANNOUNCEMENTS = "announcements"
    DOCUMENTS = "documents"
    EXPLAIN = "explain"
    CALCULATOR = "calculator"
    REALTIME = "realtime"
    FUNDAMENTALS = "fundamentals"
    GENERAL_FINANCE = "general_finance"
    OUT_OF_SCOPE = "out_of_scope"


# Asking for a list of stocks rather than about one: answered by the
# fundamentals screen. Plural or "which stock(s)", so "is TCS a good stock"
# stays a question about TCS.
_SCREEN = (
    r"\b(suggest|recommend|best|top|good|great|strong|quality|safe|some|few)\b"
    r"(?:\s+\w+){0,3}?\s+(stocks|shares|companies)\b",
    r"\b(which|what) (indian |nse |bse )?(stocks?|shares?|companies)\b"
    r".{0,30}\b(should|to|can|could) (i|we) (buy|invest|pick|choose|add)",
    r"\b(stocks?|shares?) (to|for) (buy|invest|long.term)",
    r"\bmultibaggers?\b",
    r"\b(konse|kaun ?se|kon ?se) (stocks?|shares?)\b",
    r"\bwhere (should|can) (i|we) invest\b",
)


@dataclass(frozen=True)
class Routing:
    route: Route
    reasons: tuple[str, ...]


class RouteInfo(NamedTuple):
    label: str  # the answer's badge in the chat UI
    source: str  # "how this was researched", shown under the answer
    # Code-rendered answers open with a title line the UI shows as a heading.
    titled: bool = False


# The one place a route is described; the dashboard and its /api/routes read
# this, so a new route cannot be missing from the UI or the research summary.
ROUTE_INFO: dict[Route, RouteInfo] = {
    Route.ADVICE: RouteInfo(
        "Holding review",
        "Holding review from live quote and fundamentals; no verdict.",
        titled=True,
    ),
    Route.SCREEN: RouteInfo(
        "Stock screen",
        "Fundamentals screen with stated tests; not a buy list.",
        titled=True,
    ),
    Route.HISTORY: RouteInfo(
        "Price history",
        "Upstox daily prices and live quote; returns computed.",
        titled=True,
    ),
    Route.MARKET: RouteInfo(
        "Market", "Live index and stock quotes from Upstox.", titled=True
    ),
    Route.COMPARE: RouteInfo(
        "Comparison", "Stored fundamentals and live quotes, side by side.", titled=True
    ),
    Route.RESULTS: RouteInfo(
        "Quarterly results", "Upstox quarterly income statements.", titled=True
    ),
    Route.DIVIDENDS: RouteInfo(
        "Dividends", "Upstox corporate actions and live price.", titled=True
    ),
    Route.PORTFOLIO: RouteInfo(
        "Portfolio review", "Live quotes and stored sectors; no verdict.", titled=True
    ),
    Route.TAX: RouteInfo(
        "Tax estimate", "Indian capital-gains rules, dated; live price.", titled=True
    ),
    Route.DISCOVER: RouteInfo(
        "Find companies",
        "Stored fundamentals and sectors, filtered in code.",
        titled=True,
    ),
    Route.NEWS: RouteInfo(
        "News", "Google News / Economic Times headlines, as published.", titled=True
    ),
    Route.ANNOUNCEMENTS: RouteInfo(
        "BSE filings", "BSE announcements stored from its public feed.", titled=True
    ),
    Route.DOCUMENTS: RouteInfo(
        "From the filing",
        "BSE filing PDF; answer limited to checked quotations.",
        titled=True,
    ),
    Route.EXPLAIN: RouteInfo(
        "Worked example", "Formula applied to stored figures.", titled=True
    ),
    Route.CALCULATOR: RouteInfo(
        "Calculator", "Computed from the numbers you gave.", titled=True
    ),
    Route.REALTIME: RouteInfo("Live price", "Live quote from Upstox."),
    Route.FUNDAMENTALS: RouteInfo("Fundamentals", "NSE/BSE fundamentals database."),
    Route.GENERAL_FINANCE: RouteInfo(
        "Explainer", "General finance explanation; no market data used."
    ),
    Route.OUT_OF_SCOPE: RouteInfo("Out of scope", "Scope check; no market data used."),
}


@dataclass(frozen=True)
class Intent:
    """A code-rendered feature's trigger: patterns that claim a question.

    Checked in INTENTS order after the out-of-scope check and before the
    screen/advice/model routes. Definitions ("what is a 52 week high?") stay
    education unless unless_education is False.
    """

    route: Route
    patterns: tuple[str, ...]
    unless_education: bool = True
    unless_holding: bool = False


# Ordered: earlier intents win.
INTENTS: tuple[Intent, ...] = (
    # Calculators first: "SIP of 5000 a month for 10 years at 12%" names no
    # company and every number in it is an input.
    Intent(
        Route.CALCULATOR,
        (
            r"\bsip\b.{0,40}\d",
            r"\bcagr\b.{0,40}\bfrom\b",
            r"\bposition siz",
            r"\bbreak.?even\b",
            r"\blump ?sum\b.{0,40}\d",
            r"\binvest\s+(rs\.?|₹)?\s*\d.{0,40}\b(years?|yrs?)\b",
            r"\bcalculat(e|or)\b.{0,30}\b(sip|cagr|returns?|charges|brokerage)\b",
        ),
        unless_education=False,
    ),
    Intent(
        Route.EXPLAIN,
        (
            r"\bhow (is|was|are|do you|to) .{0,40}\b(calculated|computed|derived"
            r"|worked out)\b",
            r"\b(show|explain|walk me through)\b.{0,30}\b(roe|roa|p/?e|p/?b"
            r"|margin)\b.{0,30}\b(of|for|with)\b",
        ),
        unless_education=False,
    ),
    # First: "tax if I sell my 100 TCS" is about tax, not about the holding.
    Intent(
        Route.TAX,
        (
            r"\b(ltcg|stcg|capital gains?)\b",
            r"\btax\b.{0,40}\b(sell|selling|sold|gains?|profits?|shares?|stocks?)\b",
            r"\b(sell|selling|sold)\b.{0,60}\btax(es)?\b",
        ),
    ),
    Intent(
        Route.PORTFOLIO,
        (r"\bportfolio\b", r"\bmy (holdings|investments)\b", r"\bholdings?\s*:"),
    ),
    # Before market: "Nifty 1 year return" is history, not today's level.
    Intent(
        Route.HISTORY,
        (
            r"52.?(week|wk)",
            r"\b(\d+|one|two|three|five|six|ten) ?(years?|yrs?|y|months?|m)\b.{0,20}"
            r"\b(returns?|performance|gain|change)\b",
            r"\breturns? (of|for|on|from)\b",
            r"\b(price )?(chart|graph)\b",
            r"\bprice history\b",
            r"\bhow (much )?(has|did) .{0,40}\b(moved|risen|fallen|grown|gained|lost"
            r"|performed)\b",
        ),
    ),
    Intent(
        Route.MARKET,
        (
            r"\bhow('s| is| was)? (the )?(indian |stock |share )?markets?\b(?! cap)",
            r"\bmarket (today|update|overview|summary|mood|now)\b",
            r"\b(nifty|sensex|bank ?nifty|finnifty|india vix)\b",
            r"\b(top )?(gainers|losers)\b",
            r"\b(most active|top movers)\b",
            r"\bis (the )?market (open|closed)\b",
            r"\b(stock |share )?market (holidays?|hours|timings)\b",
        ),
    ),
    # Before announcements: asking what a call said is about its content,
    # not a list of filings.
    Intent(
        Route.DOCUMENTS,
        (
            r"\b(summari[sz]e|summary|what did|what does|say|said|says|highlights?"
            r"|key points|commentary|guidance)\b.{0,60}\b(earnings call|concall"
            r"|conference call|transcript|annual report|presentation)\b",
            r"\b(earnings call|concall|conference call|transcript|annual report"
            r"|presentation)\b.{0,60}\b(say|said|says|about|summary|highlights?"
            r"|mention|on)\b",
            r"\bmanagement (said|say|says|commentary|guidance)\b",
        ),
        # "what does the annual report say" is about a filing, not a definition.
        unless_education=False,
    ),
    Intent(
        Route.ANNOUNCEMENTS,
        (
            r"\bannouncements?\b",
            r"\b(filings?|disclosures?)\b",
            r"\bboard meetings?\b",
            r"\binsider (trading|buying|selling|deals?|disclosures?)\b",
            r"\bresults? (announced|declared|filed|out) (today|this week)\b",
            r"\b(upcoming|this week'?s?) results\b",
            r"\b(results|earnings) calendar\b",
            r"\b(earnings call|concall|conference call) transcripts?\b",
        ),
    ),
    Intent(
        Route.NEWS,
        (
            r"\bnews\b",
            r"\bheadlines?\b",
            r"\bwhat'?s (happening|going on) (with|in|at)\b",
        ),
    ),
    Intent(Route.COMPARE, (r"\bcompare\b", r"\b(vs\.?|versus)\b")),
    Intent(
        Route.DISCOVER,
        (
            r"\b(stocks?|companies|shares)\b.{0,30}\b(with|having|where)\b",
            r"\b(roe|roce|roa|p/?e|p/?b|ev/?ebitda|revenue growth|sales growth"
            r"|profit growth|margins?)\b[^.,;]{0,15}\b(above|below|over|under"
            r"|more than|less than|at least|at most|between|greater than)\b",
            r"\bdebt.?free\b",
            r"\b(peers?|competitors?|rivals?)\b",
            r"\b(companies like|who competes with)\b",
            # A sector word opening the question or after a list word, so
            # "HDFC Bank shares" stays a question about HDFC Bank.
            r"(?:^\s*|\b(?:list|top|best|all|some|good|of|the|which|show|largest"
            r"|biggest|me|in)\s+)(it|bank|banking|pharma|auto|fmcg|cement|steel"
            r"|metal|power|energy|oil|telecom|realty|insurance|chemical|defence"
            r"|finance|textile|infra)s? (stocks|companies|shares|sector)\b",
            r"\bscreen\b",
        ),
        unless_holding=True,
    ),
    Intent(
        Route.RESULTS,
        (
            r"\b(latest|last|recent|quarterly|q[1-4])\b.{0,25}\b(results?|earnings"
            r"|numbers)\b",
            r"\b(results?|earnings) (of|for)\b",
        ),
    ),
    Intent(Route.DIVIDENDS, (r"\bdividends?\b",)),
)


# Asking what to do about a stock is always answered with a holding review,
# never a verdict, so these are matched before anything else and kept broad.
_ADVICE = (
    r"(?<!how )(?<!how long )\b(should|shall|can|could|must) (i|we) (buy|sell|hold"
    r"|keep|exit|invest|own|avoid|add|accumulate|average|book|continue|stay"
    r"|get out)",
    r"\bis it (wise|safe|better|okay|ok|right|a good idea|the right time|time)\b"
    r".{0,25}\b(keep|hold|sell|exit|buy|get out)",
    r"\b(would|will) you (sell|buy|hold|keep|exit)\b",
    r"(is|are) (it|this|that|they|\w+) a (good|bad|smart|safe)"
    r" (buy|investment|stock|bet)",
    r"worth (buying|selling|holding|investing)",
    r"(will|would|could)\b.{0,40}?\b(stock|shares?|share price|price)\b"
    r".{0,20}?\b(go|rise|fall|drop|climb|reach|hit|outperform|be worth)",
    r"\bcan\b.{0,40}\b(reach|hit|cross|touch)\s*(rs\.?|₹)?\s*\d",
    r"what.{0,20}(stock|shares?) (should|to) (i|we) (buy|sell|pick)",
    r"(recommend|advise)\b.{0,40}?\b(stock|shares?|buy|sell|investment|purchase)",
    r"(which|what) (?:[a-z]+\s+){0,3}(stock|shares?|company)"
    r".{0,30}(should|would) (i|we)",
    r"what should (i|we) (buy|sell|invest|own)",
    r"do you (think|believe).{0,30}(buy|sell|invest|outperform|go up|go down)",
    # Hinglish: "rakhu ya bechu", "sell kar du?", "kya karu", "lena chahiye".
    r"\b(rakhu|rakhun|rakh lu|bechu|bechun|bech du|bech dun|bechna|nikal du"
    r"|nikal dun|nikalu|kharidu|kharid lu|kharidna)\b",
    r"\b(sell|buy|hold|exit|keep|invest)\s+(karu|karun|karna|kar du|kar dun"
    r"|karein|kare)\b",
    r"\bkya (karu|karun|karein|kare)\b",
    r"\b(lena|lu|lun) chahiye\b",
    r"\bka target\b",
    r"\b(girega|badhega|chadhega)\b",
)

# Owning shares and asking what now is the core holding question, however
# vaguely the ask is put ("I own 300 Infosys shares. Thoughts?").
_HOLDING = (
    # Not after a modal: "how long should I hold shares" claims no holding.
    r"(?<!should )(?<!can )\b(i|we)('m| am|'ve| have| own| bought| hold| holding)\b"
    r".{0,40}"
    r"\b(shares?|stocks?|holdings?)\b",
    r"(?<!should )(?<!can )\b(i|we) (bought|own|hold|am holding)\b",
    r"\bmy\b.{0,30}\b(shares?|stocks?|holdings?|holding|position)\b",
    r"\bmere paas\b",
    # Money rather than a share count: "I have 2 lakh invested in Bajaj Finance".
    r"(?<!should )(?<!can )\b(i|we)('ve| have| hold| own)?\b.{0,25}"
    r"\b(invested|worth of|lakhs?|crores?)\b",
)
# Never "hold" (it is the ownership verb itself), "book" ("price to book")
# or a bare "kya" ("PE kya hai"): with those, "I hold TCS, what's the price?"
# became a holding review instead of a price lookup.
_HOLDING_ASK = (
    r"\b(advice|suggest\w*|thoughts?|review|opinion)\b",
    r"\bwhat (now|next)\b",
    r"\bwhat (to|should i|do i|shall i) do\b",
    r"\b(keep|sell|exit|trim)\b",
    # "hold" only as the question itself ("..., hold?"), never the verb of
    # owning ("I hold TCS").
    r"\bhold\s*\?",
    r"\bbook (my )?profits?\b",
    r"\b(karu|karun|chahiye)\b",
)

# Phrasings that are a holding question in the first person but a concept
# in a definition ("what does buy or sell signal mean"), so they only count
# when the question is not asking for an explanation.
_ADVICE_UNLESS_EDUCATION = (
    r"\b(keep|hold|sell|exit|buy)\b.{0,25}?\bor\b.{0,25}?"
    r"\b(keep|hold|sell|exit|buy)\b",
    r"\btime to (buy|sell|exit|book)",
    r"\bwhat (to|should (i|we)|do (i|we)) do with\b",
    r"price target",
    r"(bullish|bearish|undervalued|overvalued)",
    r"invest(ment)? advice",
)
_EDUCATION = (
    # Definitions ("what is a price target?"), not "what is TCS's target?".
    r"^\s*(what('s| is| are) (a|an)\b|what does\b|explain|define|how\b)",
    r"\b(meaning|mean|means|difference|rating|ratings|signal|strategy|explained)\b",
)

_REALTIME_PRICE = (
    r"trading at",
    r"\bcurrent price\b",
    r"\blive price\b",
    r"\bltp\b",
    r"what('s| is) .{0,30}(price|quote) (right now|currently|today)",
    r"\bprice right now\b",
)
# Plain price asks ("TCS share price", "price of reliance"). Skipped for
# definitions ("what is a stock price?"), which are education.
_REALTIME_PRICE_ASK = (
    r"\bshare price\b",
    r"\bstock price\b",
    # Not "at the price of 709": a price the user paid is not a quote request.
    r"\bprice of (?!\s*(rs\.?|inr|₹)?\s*\d)",
)

_FUNDAMENTALS = (
    r"\bp/?e\b",
    r"price.to.earnings",
    r"\broe\b",
    r"\broce\b",
    r"\broa\b",
    r"ev/ebitda",
    r"\bp/?b\b",
    r"price.to.book",
    r"book value",
    r"\bfundamentals?\b",
    r"\bcompare\b",
    r"\bvs\.?\b",
    r"revenue growth",
    r"quarterly results?",
    r"financial results?",
    r"net profit",
    r"operating profit",
    r"balance sheet",
    r"income statement",
    r"\breport\b",
    r"\banalyz(e|ing|is)\b",
    r"\bfinancials?\b",
    r"\bmargins?\b",
    r"\bgrowth\b",
    r"\bleverage\b",
    r"\bdebt\b",
    r"52.week",
    r"\bvolume\b",
    r"day change",
    r"dividend yield",
    r"\bwhich stocks\b",
    r"\bfind .{0,20}stocks\b",
    r"\bscreen\b",
    r"\bstocks with\b",
)

# A definition has no company in it, even if a word happens to match a
# listed name; "how is X doing" does not, so "how" is left out here.
_DEFINITION = r"^\s*(what('s| is| are) (a|an)\b|what does\b|explain|define)"

_GENERAL_FINANCE = (
    r"\bwhat (is|are|does|do|mean|means|meaning of)\b.{0,80}\b"
    r"(roe|roce|roa|p/?e|p/?b|eps|ebitda|market cap|dividend yield|"
    r"beta|book value|enterprise value|free cash flow|expense ratio|"
    r"stock split|bonus share|rights issue|ipo|mutual fund|etf|index fund|"
    r"bull market|bear market|support|resistance|stop loss|portfolio|"
    r"diversification|volatility|liquidity|inflation|interest rates?|"
    r"52.?week (high|low)|circuit limit|upper circuit|lower circuit)\b",
    r"\b(how (does|do|is|are)|explain|define|difference between)\b.{0,80}\b"
    r"(roe|roce|roa|p/?e|p/?b|eps|ebitda|market cap|dividend yield|"
    r"stock market|share market|mutual fund|etf|index fund|stock split|"
    r"bonus share|rights issue|ipo|portfolio|diversification|volatility|"
    r"liquidity|inflation|interest rates?)\b",
)

_FOREIGN_MARKETS = (
    r"\b(nyse|nasdaq|dow jones|s&p ?500|sec filing|10-k|10-q)\b",
    r"\b(nvidia|nvda|apple|aapl|microsoft|msft|walmart|wmt|costco)\b",
)


def _matches(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text, re.IGNORECASE)]


def route_question(
    question: str, *, names_company: bool = False, company_certain: bool = False
) -> Routing:
    """Route every question to the Indian-market data or education path.

    names_company says the caller found a listed company in the question. It
    stands in for the uppercase-symbol heuristic, which misses ordinary
    "what is the pe of infosys" phrasing, and never overrides a definition.
    company_certain says the match was not a lone everyday word: only then
    does a question with no metric at all ("how is hdfc bank doing") count as
    a company question, so "how is the global economy doing" stays general.
    """
    text = question.strip()
    if not text:
        return Routing(Route.GENERAL_FINANCE, ("empty question",))

    # Foreign first: a buy/sell question about Apple is still out of scope,
    # and the advice path only has NSE/BSE data to review it with.
    foreign = _matches(text, _FOREIGN_MARKETS)
    if foreign:
        return Routing(
            Route.OUT_OF_SCOPE, tuple(f"outside_india:{item}" for item in foreign)
        )

    # Intents step aside only for real definitions ("what is a 52 week
    # high?"), not every "how" ("how is the market today?").
    definition_cue = bool(re.search(_DEFINITION, text, re.IGNORECASE))
    holding_cue = bool(_matches(text, _HOLDING))
    for intent in INTENTS:
        if intent.unless_education and definition_cue:
            continue
        if intent.unless_holding and holding_cue:
            continue
        found = _matches(text, intent.patterns)
        if found:
            return Routing(
                intent.route, tuple(f"{intent.route.value}:{p}" for p in found)
            )

    # Before advice: "which stock should I pick?" names no company to review.
    screen = [] if _matches(text, _HOLDING) else _matches(text, _SCREEN)
    if screen and not _matches(text, _EDUCATION):
        return Routing(Route.SCREEN, tuple(f"screen:{item}" for item in screen))

    advice = _matches(text, _ADVICE)
    if not _matches(text, _EDUCATION):
        if _matches(text, _HOLDING):
            advice += _matches(text, _HOLDING_ASK)
        advice += _matches(text, _ADVICE_UNLESS_EDUCATION)
    if advice:
        return Routing(Route.ADVICE, tuple(f"advice:{item}" for item in advice))

    realtime = _matches(text, _REALTIME_PRICE)
    if realtime:
        return Routing(Route.REALTIME, tuple(f"realtime:{item}" for item in realtime))

    if not _matches(text, _EDUCATION):
        price_ask = _matches(text, _REALTIME_PRICE_ASK)
        if price_ask:
            return Routing(
                Route.REALTIME, tuple(f"realtime:{item}" for item in price_ask)
            )

    fundamentals = _matches(text, _FUNDAMENTALS)
    metric_terms = {"ROE", "ROCE", "ROA", "EPS", "EBITDA", "LTP", "NSE", "BSE"}
    is_definition = bool(re.search(_DEFINITION, text, re.IGNORECASE))
    has_company = (names_company and not is_definition) or any(
        token not in metric_terms for token in re.findall(r"\b[A-Z]{2,}\b", text)
    )
    education = _matches(text, _GENERAL_FINANCE)
    if fundamentals and (not education or has_company):
        return Routing(
            Route.FUNDAMENTALS,
            tuple(f"fundamentals:{item}" for item in fundamentals),
        )

    if education:
        return Routing(
            Route.GENERAL_FINANCE,
            tuple(f"general_finance:{item}" for item in education),
        )

    if names_company and company_certain and not is_definition:
        # "how is hdfc bank doing": no metric named, but a company is.
        return Routing(Route.FUNDAMENTALS, ("fundamentals:company named",))

    return Routing(Route.GENERAL_FINANCE, ("general Indian-market question",))
