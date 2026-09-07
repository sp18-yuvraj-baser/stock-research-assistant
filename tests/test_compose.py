from sra.agent.compose import (
    ADVICE_REFUSAL,
    FIGURES_HEADING,
    NARRATIVE_HEADING,
    compose_hybrid,
)
from sra.agent.loop import PathResult
from sra.tools.run_sql import SqlResult
from sra.tools.search_filings import Passage, SearchResult


def _numeric(accession: str = "0001045810-26-000075") -> PathResult:
    return PathResult(
        text="Gross margin was 74.98%.",
        sql_calls=[
            SqlResult(
                sql="SELECT ...",
                columns=["accession_no", "fiscal_year", "fiscal_period", "period_end"],
                rows=[
                    {
                        "accession_no": accession,
                        "fiscal_year": 2027,
                        "fiscal_period": "Q2",
                        "period_end": "2026-07-26",
                    }
                ],
            )
        ],
    )


def _narrative(accession: str = "0001045810-26-000075") -> PathResult:
    return PathResult(
        text='Margin rose "due to improved mix from Blackwell Ultra".',
        search_calls=[
            SearchResult(
                query="gross margin",
                passages=[
                    Passage(
                        ticker="NVDA",
                        form_type="10-Q",
                        period_of_report="2026-07-26",
                        accession_no=accession,
                        section="Part I Item 2 Management's Discussion and Analysis",
                        ordinal=3,
                        text="Gross margin increased due to improved mix.",
                        similarity=0.81,
                    )
                ],
            )
        ],
    )


def test_both_sources_appear_under_separate_headings() -> None:
    composed = compose_hybrid(_numeric(), _narrative())
    assert FIGURES_HEADING in composed
    assert NARRATIVE_HEADING in composed
    # The figure must sit above the narrative heading, never inside it.
    assert composed.index("74.98%") < composed.index(NARRATIVE_HEADING)
    assert composed.index("Blackwell Ultra") > composed.index(NARRATIVE_HEADING)


def test_each_block_carries_its_own_sources() -> None:
    composed = compose_hybrid(
        _numeric("0000000001-01-000001"), _narrative("0000000002-02-000002")
    )
    figures, narrative = composed.split(NARRATIVE_HEADING)
    assert "0000000001-01-000001" in figures
    assert "0000000001-01-000001" not in narrative
    assert "0000000002-02-000002" in narrative


def test_mismatched_periods_are_flagged() -> None:
    # Both halves can be individually correct while the composition misleads.
    composed = compose_hybrid(
        _numeric("0000000001-01-000001"), _narrative("0000000002-02-000002")
    )
    assert "may describe a different period" in composed


def test_shared_filing_is_not_flagged() -> None:
    composed = compose_hybrid(_numeric(), _narrative())
    assert "may describe a different period" not in composed


def test_empty_numeric_path_says_so_rather_than_going_quiet() -> None:
    composed = compose_hybrid(PathResult(), _narrative())
    assert "No figures for this were found" in composed
    assert NARRATIVE_HEADING in composed


def test_empty_narrative_path_says_so() -> None:
    composed = compose_hybrid(_numeric(), PathResult())
    assert "do not appear to explain this" in composed


def test_advice_refusal_offers_an_alternative() -> None:
    assert "not investment advice" in ADVICE_REFUSAL
    assert "What it can do instead" in ADVICE_REFUSAL
