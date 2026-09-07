"""Tests for the scoring itself.

A metric that miscounts is worse than no metric, so the extractor's own
precision is tested against the shapes real answers actually contain.
"""

from decimal import Decimal

import pytest

from sra.eval.checks import (
    EXACT,
    QUOTED,
    ROUNDED,
    UNTRACEABLE,
    cited_accessions,
    classify_figures,
    extract_figures,
    has_unbalanced_quotes,
    looks_like_refusal,
    normalize_quote,
    quote_is_supported,
    quoted_spans,
    sections_hit,
    value_match,
)


def _statuses(answer: str, values: set[Decimal], passages: list[str]) -> list[str]:
    return [v.status for v in classify_figures(answer, values, passages)]


@pytest.mark.parametrize(
    "text",
    [
        "accession 0001045810-26-000021",
        "for the period ending 2026-07-26",
        "see Item 1A Risk Factors",
        "in Item 7",
        "Part II Item 7",
        "revenue in FY2025 grew",
        "during fiscal year 2026",
        "in fiscal 2025",
        "for Q2 2027",
        "in Q3",
        "reported in 2025",
    ],
)
def test_identifiers_and_period_labels_are_not_figures(text: str) -> None:
    # Left in, these swamp the real figures and make the metric meaningless.
    assert extract_figures(text) == []


@pytest.mark.parametrize("text", ["the H20 chip", "GB200 systems", "RTX50 cards"])
def test_digits_glued_to_letters_are_product_names(text: str) -> None:
    assert extract_figures(text) == []


@pytest.mark.parametrize(
    "text",
    [
        # Every one of these produced a false hallucination in the first
        # baseline, together accounting for 10 of 12 reported failures.
        "quote text from the company's SEC filings (10-K and 10-Q)",
        "a question about their 10-K or 10-Q filings",
        "the Standardization 31000:2018 Risk Management Standard",
        "aligning with ISO 27001 controls",
        "the period ending September 29, 2018",
        "for the three months ended October 31, 2025",
        "the fiscal year ended August 31, 2025",
        "as of 30 September",
        # A statement note reference, not a quantity.
        "details in Note 9-Net Income per Common Share",
        "see Notes 12 and 13",
    ],
)
def test_form_names_standards_and_prose_dates_are_not_figures(text: str) -> None:
    assert extract_figures(text) == []


def test_a_real_figure_beside_a_prose_date_still_counts() -> None:
    figures = extract_figures(
        "For the quarter ended August 31, 2025, revenue was 96,221,000,000."
    )
    assert [f.text for f in figures] == ["96,221,000,000"]


def test_exact_match_to_a_sql_result() -> None:
    answer = "Revenue was 130,497,000,000."
    assert _statuses(answer, {Decimal("130497000000")}, []) == [EXACT]


def test_percentage_compares_at_face_value() -> None:
    # SQL returns 74.98 for a margin the answer writes as 74.98%.
    assert _statuses("Margin was 74.98%.", {Decimal("74.98")}, []) == [EXACT]


def test_scaled_wording_resolves_to_the_underlying_value() -> None:
    answer = "Revenue was $130.497 billion."
    assert _statuses(answer, {Decimal("130497000000")}, []) == [EXACT]


def test_figure_restated_at_lower_precision_is_rounded_not_invented() -> None:
    answer = "Revenue was about $130.5 billion."
    assert _statuses(answer, {Decimal("130497000000")}, []) == [ROUNDED]


def test_quoted_filing_figure_is_allowed_when_the_passage_contains_it() -> None:
    answer = 'The filing says margins "decreased to 73.4% for the quarter".'
    passages = ["Gross margins decreased to 73.4% for the quarter, compared to"]
    assert _statuses(answer, set(), passages) == [QUOTED]


def test_quoted_figure_absent_from_every_passage_is_untraceable() -> None:
    # The quotation marks must not launder a number nothing returned.
    answer = 'The filing says margins "decreased to 61.2% for the quarter".'
    passages = ["Gross margins decreased to 73.4% for the quarter"]
    assert _statuses(answer, set(), passages) == [UNTRACEABLE]


def test_unquoted_figure_from_a_passage_is_still_untraceable() -> None:
    # Figures must come from run_sql; prose is not a source of figures.
    answer = "Gross margin was 73.4% last quarter."
    passages = ["Gross margins decreased to 73.4% for the quarter"]
    assert _statuses(answer, set(), passages) == [UNTRACEABLE]


def test_invented_figure_is_untraceable() -> None:
    answer = "Revenue was 999,999,999,999."
    assert _statuses(answer, {Decimal("130497000000")}, []) == [UNTRACEABLE]


def test_value_match_reports_the_relationship() -> None:
    assert value_match(Decimal("100"), Decimal("100")) == EXACT
    assert value_match(Decimal("100.4"), Decimal("100")) == ROUNDED
    assert value_match(Decimal("120"), Decimal("100")) is None


def test_accessions_are_extracted_for_citation_checking() -> None:
    answer = "See 0001045810-26-000021 and 0000320193-25-000079."
    assert cited_accessions(answer) == {
        "0001045810-26-000021",
        "0000320193-25-000079",
    }


def test_unbalanced_quotes_are_detected() -> None:
    assert has_unbalanced_quotes('He said "one thing and left it open.')
    assert not has_unbalanced_quotes('He said "one thing" and closed it.')


def test_gaps_between_quotations_are_not_treated_as_quotations() -> None:
    # An unpaired quote earlier in the answer shifts every later pair, so the
    # text between two quotations gets captured instead of the quotations.
    answer = (
        'Controls "may disrupt our supply chain" (Item 1A Risk Factors, '
        '0001045810-26-000021).\n*   They may also "encourage design-out".'
    )
    spans = quoted_spans(answer)
    assert "may disrupt our supply chain" in spans
    assert not any("Item 1A" in span for span in spans)
    assert not any("\n" in span for span in spans)


def test_section_matching_is_by_containment() -> None:
    assert sections_hit(["Item 1A"], ["Part I Item 1A Risk Factors"]) == ["Item 1A"]
    assert sections_hit(["Item 7"], ["Part I Item 2 Management's Discussion"]) == []


@pytest.mark.parametrize(
    "answer",
    [
        "That figure is not available in the filings.",
        "Nvidia does not appear to discuss this.",
        "I cannot find Tesla's CIK in the available data.",
        "The filings do not carry market prices.",
        "This is not investment advice.",
    ],
)
def test_refusals_are_recognised(answer: str) -> None:
    assert looks_like_refusal(answer)


def test_a_confident_answer_is_not_a_refusal() -> None:
    assert not looks_like_refusal("Revenue was 130,497,000,000 in FY2025.")


def test_a_note_reference_beside_a_figure_keeps_the_figure() -> None:
    figures = extract_figures("Note 9 states net income was 8,099,000,000.")
    assert [f.text for f in figures] == ["8,099,000,000"]


@pytest.mark.parametrize(
    ("quote", "passage"),
    [
        # Quoting conventions a faithful quotation is allowed to use. Each of
        # these was reported unverified in the second baseline.
        (
            "integral to our business and profitability,",
            "Membership fees are integral to our business and profitability.",
        ),
        (
            "underestimate[s] demand, and our foundry partners",
            "if we underestimate demand, and our foundry partners are unable",
        ),
        (
            "Litigation (MDL No. 2804) (the 'MDL') is pending",
            "Litigation (MDL No. 2804) (the \u201cMDL\u201d) is pending in the U.S.",
        ),
        (
            "Advancing  the NVIDIA   accelerated computing platform",
            "Advancing the NVIDIA accelerated computing platform, including",
        ),
    ],
)
def test_faithful_quotations_are_accepted(quote: str, passage: str) -> None:
    assert quote_is_supported(quote, [passage])


@pytest.mark.parametrize(
    ("quote", "passage"),
    [
        # Normalisation must not launder a fabrication.
        (
            "margins decreased to 61.2% for the quarter",
            "Gross margins decreased to 73.4% for the quarter",
        ),
        ("we plan to open a theme park", "Our data center revenue grew."),
        ("demand was weak and falling", "Demand was strong and rising."),
    ],
)
def test_fabricated_quotations_still_fail(quote: str, passage: str) -> None:
    assert not quote_is_supported(quote, [passage])


def test_a_quotation_with_nothing_retrieved_cannot_be_supported() -> None:
    assert not quote_is_supported("anything at all", [])


def test_normalisation_keeps_the_words() -> None:
    # Quote marks are dropped from both sides; the words must survive.
    assert (
        normalize_quote("  the \u201cquick\u201d brown fox,  ") == "the quick brown fox"
    )
