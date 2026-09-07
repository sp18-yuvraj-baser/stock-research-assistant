"""Automatic checks over a generated answer.

Scored mechanically, with no LLM judge and no hand grading, so the scoreboard
is reproducible.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

EXACT = "exact"
ROUNDED = "rounded"
QUOTED = "quoted"
UNTRACEABLE = "untraceable"

# Tolerance for a figure that traces to a real value but was restated at lower
# precision ("$130.5 billion" for 130,497,000,000). Reported separately from
# untraceable: rounding is a different fault from invention, and folding the two
# together would make the headline metric unreadable.
ROUNDING_TOLERANCE = Decimal("0.005")

# Strings that look numeric but are identifiers or period labels, not figures.
# Left in place they would swamp the real figures and make the metric useless.
_NOT_A_FIGURE = (
    r"\d{10}-\d{2}-\d{6}",  # accession numbers
    r"\d{4}-\d{2}-\d{2}",  # ISO dates
    r"\bitems?\s*\d{1,2}[a-c]?\b",  # Item 1A, Item 7
    r"\bpart\s+[iv]+\b",
    r"\bfy\s*\d{4}\b",
    r"\bfiscal\s+(year|quarter)s?\s+\d{4}\b",
    r"\bfiscal\s+\d{4}\b",
    r"\bq[1-4]\s*(fy)?\s*\d{4}\b",
    r"\bq[1-4]\b",  # a bare quarter label
    # Form names: "10-K" and "10-Q" otherwise read as the figure 10, which was
    # the single largest source of false positives in the first baseline.
    r"\b(10-k|10-q|8-k|20-f|40-f|s-1|s-3|6-k|11-k)(/a)?\b",
    # Dates written out in prose. Only ISO dates were masked at first, so
    # "September 29, 2018" contributed the figure 29.
    r"\b(january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\s+\d{1,2}(st|nd|rd|th)?,?\s*((19|20)\d{2})?\b",
    r"\b\d{1,2}(st|nd|rd|th)?\s+(january|february|march|april|may|june|july"
    r"|august|september|october|november|december)\b",
    # Named standards and frameworks carry numbers that are identifiers:
    # "ISO 31000:2018" is not a figure.
    r"\b(iso|iec|ieee|nist sp|sox|asc|ifrs|fasb|sfas)\s*\d+(:\d+)?(-\d+)?\b",
    # A colon-year suffix marks a standard regardless of what introduces it:
    # "Standardization 31000:2018" names ISO 31000, not a quantity.
    r"\b\d+:(19|20)\d{2}\b",
    r"\b(19|20)\d{2}\b",  # a bare year
)

_SCALES = {
    "trillion": Decimal(10) ** 12,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "million": Decimal(10) ** 6,
    "thousand": Decimal(10) ** 3,
}

# The leading lookbehind rejects digits glued to letters: H20, GB200 and RTX50
# are product names, and counting them as figures would swamp the metric.
_FIGURE = re.compile(
    r"(?<![A-Za-z0-9])\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*"
    r"(trillion|billion|bn|million|thousand)?\s*(%)?",
    re.IGNORECASE,
)

# Straight and typographic quotation marks; filings use the typographic ones.
_QUOTED_SPAN = re.compile(r"\"[^\"]{2,}\"|“[^”]{2,}”")


@dataclass(frozen=True)
class Figure:
    text: str
    value: Decimal
    quoted: bool


@dataclass(frozen=True)
class FigureVerdict:
    figure: Figure
    status: str
    matched: str | None = None


def _mask_non_figures(text: str) -> str:
    """Blank identifiers and period labels, preserving offsets."""
    masked = text
    for pattern in _NOT_A_FIGURE:
        masked = re.sub(
            pattern, lambda m: " " * len(m.group(0)), masked, flags=re.IGNORECASE
        )
    return masked


def _quoted_ranges(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _QUOTED_SPAN.finditer(text)]


def extract_figures(answer: str) -> list[Figure]:
    """Pull the figures an answer asserts, ignoring identifiers and periods."""
    masked = _mask_non_figures(answer)
    quoted = _quoted_ranges(answer)
    figures: list[Figure] = []
    for match in _FIGURE.finditer(masked):
        whole, fraction, scale, _percent = match.groups()
        if whole is None:
            continue
        try:
            value = Decimal(whole.replace(",", "") + (fraction or ""))
        except InvalidOperation:
            continue
        if scale:
            value *= _SCALES[scale.lower()]
        start = match.start()
        figures.append(
            Figure(
                text=answer[match.start() : match.end()].strip(),
                value=value,
                # A percentage keeps its face value: SQL returns 74.98 for
                # 74.98%, so the two compare directly.
                quoted=any(lo <= start < hi for lo, hi in quoted),
            )
        )
    return figures


def _is_rounded(answer_value: Decimal, source: Decimal) -> bool:
    if source == 0:
        return answer_value == 0
    return abs(answer_value - source) / abs(source) <= ROUNDING_TOLERANCE


def classify_figure(
    figure: Figure,
    sql_values: set[Decimal],
    passages: list[str],
) -> FigureVerdict:
    """Trace one figure back to evidence, or report that it cannot be."""
    for value in sql_values:
        if figure.value == value:
            return FigureVerdict(figure=figure, status=EXACT, matched=str(value))

    # A figure inside a verbatim quotation is the filing's own number, allowed
    # only if it really appears in a passage that was retrieved.
    if figure.quoted:
        digits = figure.text.lstrip("$ ").strip()
        if any(digits in passage for passage in passages):
            return FigureVerdict(figure=figure, status=QUOTED, matched=digits)

    for value in sql_values:
        if _is_rounded(figure.value, value):
            return FigureVerdict(figure=figure, status=ROUNDED, matched=str(value))

    return FigureVerdict(figure=figure, status=UNTRACEABLE)


def value_match(answer_value: Decimal, source: Decimal) -> str | None:
    """How an asserted figure relates to a known value, if at all.

    Compares normalised values, so it is independent of how the figure was
    written or of where the answer got it from.
    """
    if answer_value == source:
        return EXACT
    if _is_rounded(answer_value, source):
        return ROUNDED
    return None


def classify_figures(
    answer: str, sql_values: set[Decimal], passages: list[str]
) -> list[FigureVerdict]:
    return [
        classify_figure(figure, sql_values, passages)
        for figure in extract_figures(answer)
    ]


def cited_accessions(answer: str) -> set[str]:
    return set(re.findall(r"\d{10}-\d{2}-\d{6}", answer))


# A model that leaves an odd number of quotation marks misaligns every pair
# after it, so naive pairing then captures the gaps between quotations instead
# of the quotations. These reject those artifacts: a quotation lifted from a
# filing is one run of prose, never spanning a line break and never containing
# the citation parenthetical that follows it.
_MIN_QUOTE_CHARS = 8


def has_unbalanced_quotes(answer: str) -> bool:
    """Whether quotation marks pair up. An odd count is a real quality fault."""
    straight = answer.count('"')
    return straight % 2 == 1 or answer.count("\u201c") != answer.count("\u201d")


def quoted_spans(answer: str) -> list[str]:
    """Quotations the answer presents as verbatim filing text."""
    spans: list[str] = []
    for match in _QUOTED_SPAN.finditer(answer):
        span = match.group(0)[1:-1].strip()
        if len(span) < _MIN_QUOTE_CHARS:
            continue
        if "\n" in span or "(Item " in span:
            continue
        if re.search(r"\d{10}-\d{2}-\d{6}", span):
            continue
        spans.append(span)
    return spans


def sections_hit(expected: list[str], retrieved: list[str]) -> list[str]:
    """Which expected Item sections appear among the retrieved ones."""
    return [
        want
        for want in expected
        if any(want.lower() in got.lower() for got in retrieved)
    ]


_REFUSAL_MARKERS = (
    r"not available",
    r"do(es)? not appear",
    r"cannot (find|locate|retrieve|provide|determine)",
    r"could not (find|locate|retrieve)",
    r"unable to (find|locate|retrieve|determine)",
    r"no (data|information|figures?|filings?|mention|record)",
    r"not (present|found|covered|disclosed|included)",
    r"is not among",
    r"do not (carry|cover|contain|include)",
    r"not investment advice",
    r"outside the scope",
)


def looks_like_refusal(answer: str) -> bool:
    """Whether an answer declines rather than asserting something.

    Keyword-based on purpose: a refusal that a keyword scan cannot recognise is
    unlikely to read as a clear refusal to a person either.
    """
    return any(re.search(p, answer, re.IGNORECASE) for p in _REFUSAL_MARKERS)
