"""Find the listed company (or companies) a question names.

Shared by every answer that is about a company: the holding review, price
history, comparisons and the router's company check. Matching is done on the
question's own words against stored listing names and symbols; lone everyday
words ("global", "action") are weak matches that never beat a confident one.
"""

import re
from dataclasses import dataclass, field

import psycopg

from sra.upstox.instruments import EquityInstrument, match_instruments

# Words that occur in questions but never start or end a company name in
# them. Without this, "time" resolves to TIME TECHNOPLAST and "idea"
# to VODAFONE IDEA.
_STOPWORDS = frozenset(
    """
    i me my we our you your it its is am are was be been have has had do
    does did should shall can could would will must may a an the and or but
    if of to in on at for with from by about into as than then so this that
    these those them they their there here what which who how when why where
    any some all keep hold holding holdings sell buy exit add book booking
    profit profits loss losses average accumulate continue stay avoid invest
    investment investing own owned bought buying selling purchase share
    shares stock stocks equity position portfolio advice suggest suggestion
    suggestions thoughts review now today right good bad wise safe better
    time long term short price target market value worth go up down rise
    fall get out year years month months rs inr crore lakh quantity qty plan
    lot more less trim ok okay please tell think idea view also just still
    yet already nse bse kya mujhe mere paas hai ka ki ke ya karu karun karna
    kar du dun chahiye rakhu rakhna bechu bechna nikal lena sahi abhi mein
    dividend dividends yield yields payer payers paying high low highest
    lowest chart graph history return returns week weeks performance result
    results quarter quarterly earnings compare versus vs peer peers
    competitor competitors rival rivals sector sectors company companies list
    top best gainers losers active movers market markets index tax taxes gain
    gains capital ltcg stcg screen growth margin margins debt free above below
    under over between ratio ratios latest
    """.split()  # noqa: SIM905 -- a word list reads better as prose
)

# Longest company name, in words, worth matching ("state bank of india").
_MAX_NGRAM = 4

# Everyday names that are neither the NSE symbol nor the start of the listed
# name ("SBI" is SBIN, "L&T" is LARSEN & TOUBRO, Zomato is now ETERNAL).
_ALIASES = {
    "sbi": "SBIN",
    "l&t": "LT",
    "lnt": "LT",
    "zomato": "ETERNAL",
    "hul": "HINDUNILVR",
    "airtel": "BHARTIARTL",
    "kotak bank": "KOTAKBANK",
    "ril": "RELIANCE",
}

# Digit runs are bounded so a pathological message cannot make matching slow.


def _candidate_phrases(question: str) -> list[tuple[range, str]]:
    """Word n-grams that could name a company, longest first, with positions."""
    words = re.findall(r"[a-z0-9&]+", question.lower())
    phrases: list[tuple[range, str]] = []
    for size in range(min(_MAX_NGRAM, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            gram = words[start : start + size]
            if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                continue
            if gram[0].isdigit():
                continue
            phrases.append((range(start, start + size), " ".join(gram)))
    return phrases


@dataclass(frozen=True)
class Resolution:
    companies: list[EquityInstrument]
    # Several listed names start with the phrase and none was named exactly.
    ambiguous: list[EquityInstrument]
    error: str | None = None
    # Matches set aside because a stronger one was found; named in the
    # answer so a second company is never dropped silently.
    set_aside: list[EquityInstrument] = field(default_factory=list)
    # The companies came from confident matches, not a lone lowercase word.
    confident: bool = False


def resolve_companies(question: str) -> Resolution:
    """Find every NSE company a question names.

    Longer phrases win and claim their words, so "hdfc bank" is one company,
    not HDFC Bank plus whatever "bank" alone would match.
    """
    phrases = _candidate_phrases(question)
    if not phrases:
        return Resolution([], [])
    keys = {phrase: _ALIASES.get(phrase, phrase.replace(" ", "").upper())
            for _, phrase in phrases}  # fmt: skip
    try:
        rows = match_instruments(
            tickers=sorted(set(keys.values())),
            names=[p.upper() for _, p in phrases],
            prefixes=[f"{p.upper()} %" for _, p in phrases],
        )
    except psycopg.Error as exc:
        return Resolution([], [], error=str(exc).strip())
    # A single lowercase word is weak: "global" is a ticker and "action" starts
    # a listed name, but in "the global slowdown" or "the RBI action" neither
    # names a company. It counts only when nothing stronger matched.
    strong: list[EquityInstrument] = []
    weak: list[EquityInstrument] = []
    ambiguous: list[EquityInstrument] = []
    claimed: set[int] = set()
    for words, phrase in phrases:
        if claimed.intersection(words):
            continue
        upper = phrase.upper()
        exact = [c for c in rows if c.ticker == keys[phrase] or c.name == upper]
        named = exact or [c for c in rows if c.name.startswith(f"{upper} ")]
        if len(named) == 1:
            found = strong if _confident(question, phrase, words) else weak
            if named[0] not in found:
                found.append(named[0])
            claimed.update(words)
        elif named:
            ambiguous = ambiguous or sorted(named, key=lambda c: c.ticker)[:5]
            claimed.update(words)
    if strong:
        others = [c for c in weak + ambiguous if c not in strong]
        return Resolution(strong, [], set_aside=others, confident=True)
    if weak:
        return Resolution(weak, [], set_aside=[c for c in ambiguous if c not in weak])
    return Resolution([], ambiguous)


def _confident(question: str, phrase: str, words: range) -> bool:
    """Whether a matched phrase surely names a company rather than a word.

    True for several words ("hdfc bank"), a known alias ("sbi"), a symbol typed
    in capitals ("TCS"), or a word used like a holding ("my infosys shares",
    "the pe of infosys").
    """
    if len(words) > 1 or phrase in _ALIASES:
        return True
    if re.search(rf"\b{re.escape(phrase.upper())}\b", question):
        return True
    lower = question.lower()
    word = re.escape(phrase)
    return bool(
        re.search(rf"\b(my|of|in|sell|buy|hold|keep)\s+{word}\b", lower)
        or re.search(rf"\b{word}\s+(shares?|stocks?|ltd|limited)\b", lower)
    )
