"""Tests for section-filter behavior in search_filings.

Requires a live database with the narrative path indexed, unlike the other
eval/router tests -- run only where the local dev DB is reachable.
"""

import pytest

from sra.tools.search_filings import _normalize_section, search_filings


@pytest.mark.parametrize(
    ("alias", "expected_fragment"),
    [
        ("MD&A", "discussion and analysis"),
        ("md&a", "discussion and analysis"),
        ("MDNA", "discussion and analysis"),
    ],
)
def test_known_abbreviations_normalize_to_the_canonical_fragment(
    alias: str, expected_fragment: str
) -> None:
    # Canonical section labels read "Management's Discussion and Analysis";
    # "MD&A" is not a substring of that, so filtering on it verbatim silently
    # matched zero passages even with a correct ticker and topic.
    assert expected_fragment in _normalize_section(alias)


def test_unrecognised_section_text_passes_through_unchanged() -> None:
    assert _normalize_section("Item 7") == "Item 7"
    assert _normalize_section("Risk Factors") == "Risk Factors"


def test_md_and_a_alias_finds_passages_a_literal_filter_would_miss() -> None:
    with_alias = search_filings("Services revenue", ticker="AAPL", section="MD&A")
    assert with_alias.error is None
    assert with_alias.passages, "the alias should resolve to real MD&A passages"
    assert all(
        "discussion and analysis" in p.section.lower() for p in with_alias.passages
    )
