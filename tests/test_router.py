import pytest

from sra.agent.router import Route, route_question


@pytest.mark.parametrize(
    "question",
    [
        "What was Nvidia's revenue in FY2025?",
        "What was Apple's gross margin last quarter?",
        "Show me Nvidia's gross margin over the last 8 quarters.",
        "How much did Microsoft spend on R&D in FY2025?",
        "What were Walmart's total assets at the end of FY2025?",
        "What is Costco's inventory balance?",
        "How many shares outstanding does Nvidia have?",
        "What was Apple's EPS last year?",
        "How did Nvidia's gross margin change year over year?",
    ],
)
def test_numeric_questions(question: str) -> None:
    assert route_question(question).route is Route.NUMERIC


@pytest.mark.parametrize(
    "question",
    [
        "What risks does Nvidia cite around export controls?",
        "What does Apple say about competition?",
        "How does Microsoft describe its cybersecurity risk?",
        "What legal proceedings is Walmart involved in?",
        "What does Costco disclose about its supply chain?",
        "What is Nvidia's stated strategy for data centers?",
        # "year over year" here frames the comparison of risk factors; it is not
        # a request for a figure.
        "Which risk factors changed year over year for Nvidia?",
    ],
)
def test_narrative_questions(question: str) -> None:
    assert route_question(question).route is Route.NARRATIVE


@pytest.mark.parametrize(
    "question",
    [
        "Why did Nvidia's gross margin decline last quarter?",
        "Why did revenue grow so fast in FY2025?",
        "What drove the increase in Microsoft's operating income?",
        "Explain the change in Walmart's margins.",
        "How did Nvidia's gross margin change and what does management say?",
        "What caused the drop in Apple's net income?",
    ],
)
def test_hybrid_questions_need_both_paths(question: str) -> None:
    assert route_question(question).route is Route.HYBRID


@pytest.mark.parametrize(
    "question",
    [
        "Should I buy NVDA stock?",
        "Is Apple a good investment right now?",
        "Will the stock go up next quarter?",
        "What's your price target for Nvidia?",
        "Do you think Microsoft will outperform?",
        "Is Costco overvalued?",
        "Would you recommend buying Walmart?",
        "Is now a good time to buy semiconductors?",
        "Should I sell my Nvidia shares before earnings?",
        "Is NVDA undervalued at these levels?",
    ],
)
def test_advice_is_always_caught(question: str) -> None:
    # A missed advice question is the one failure with no acceptable rate.
    assert route_question(question).route is Route.ADVICE


def test_advice_wins_over_a_numeric_question() -> None:
    # Wrapping a recommendation in a figure request must not evade the check.
    routing = route_question(
        "Nvidia's revenue grew 100% -- should I buy the stock?"
    )
    assert routing.route is Route.ADVICE


def test_routing_reports_why() -> None:
    routing = route_question("Why did Nvidia's gross margin decline?")
    assert routing.route is Route.HYBRID
    assert any("causal" in reason for reason in routing.reasons)
    assert any("numeric" in reason for reason in routing.reasons)


def test_unrecognised_question_defaults_to_filing_text() -> None:
    routing = route_question("Who is on Nvidia's board?")
    assert routing.route is Route.NARRATIVE


def test_empty_question_does_not_crash() -> None:
    assert route_question("   ").route is Route.NARRATIVE
