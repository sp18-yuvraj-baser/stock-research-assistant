import pytest

from sra.agent.router import Route, route_question


@pytest.mark.parametrize(
    "question",
    [
        "What's TCS trading at right now?",
        "What is the current price of Infosys?",
        "What's RELIANCE's LTP?",
        "What is ABCIND's live price?",
    ],
)
def test_realtime_questions_cover_any_synced_indian_symbol(question: str) -> None:
    assert route_question(question).route is Route.REALTIME


@pytest.mark.parametrize(
    "question",
    [
        "What is HDFC Bank's P/E ratio?",
        "Show Infosys revenue growth for 5 years.",
        "What is the latest quarterly result of TCS?",
        "Which NSE stocks have ROE > 20%?",
        "Compare TCS vs Infosys.",
        "Analyze ABCIND fundamentally.",
    ],
)
def test_fundamentals_questions_use_the_indian_equity_path(question: str) -> None:
    assert route_question(question).route is Route.FUNDAMENTALS


@pytest.mark.parametrize(
    "question",
    [
        "Should I buy TCS?",
        "Is ABCIND a good investment right now?",
        "What is TCS's price target?",
        "Which Indian stock should I pick?",
        "I have 100 hdfc bank stocks, should I keep or sell them?",
        "Should I keep my TCS shares?",
        "What should I do with my 50 INFY shares?",
        "Is it time to exit Reliance?",
        "Hold or sell ITC?",
        "Should we book profits in HDFCBANK?",
        "Shall I sell my HDFC Bank shares?",
        "Is it safe to hold ICICI bank?",
        "I'm holding 200 SBIN, any advice",
        "I own 300 Infosys shares. Thoughts?",
        "I bought TCS at 3500, what now?",
        "my reliance holding is down 20%, review it",
        "hdfc bank rakhu ya bechu",
        "hdfc bank sell kar du?",
        "mere paas 100 reliance shares hai kya karu",
        "can hdfc bank reach 2000",
        "I have 100 hdfc bank stocks at average cost 1500, should I keep them?",
        "I have 2 lakh invested in Bajaj Finance, hold?",
        "Is TCS overvalued?",
    ],
)
def test_advice_is_always_caught(question: str) -> None:
    assert route_question(question).route is Route.ADVICE


@pytest.mark.parametrize(
    "question",
    [
        "What is the meaning of ROE?",
        "Explain the P/E ratio.",
        "What is an ETF?",
        "What is the difference between a bull market and a bear market?",
        "What does buy or sell signal mean?",
        "Explain buy or hold strategy.",
        "How do I buy or sell shares in a demat account?",
        "How long should I hold shares for LTCG?",
        "What is the right time to buy a stock, explained?",
        "What does bullish mean?",
        "What is a price target?",
        "How do I sell my shares on Zerodha?",
        "How is the global economy doing?",
    ],
)
def test_general_finance_questions(question: str) -> None:
    assert route_question(question).route is Route.GENERAL_FINANCE


@pytest.mark.parametrize(
    "question",
    [
        "What was Nvidia's revenue in FY2025?",
        "What is Apple's P/E ratio?",
        "Show NASDAQ stocks with high ROE.",
        "Should I sell my Apple shares?",
    ],
)
def test_foreign_market_questions_are_refused_as_out_of_scope(question: str) -> None:
    assert route_question(question).route is Route.OUT_OF_SCOPE


@pytest.mark.parametrize(
    "question",
    [
        "what is the pe of infosys",
        "What is Infosys ROE?",
        "how is hdfc bank doing",
    ],
)
def test_a_company_named_in_plain_words_reaches_the_data_path(question: str) -> None:
    # The regexes alone cannot know "infosys" is a company; the caller does.
    assert route_question(question).route is Route.GENERAL_FINANCE
    routing = route_question(question, names_company=True, company_certain=True)
    assert routing.route is Route.FUNDAMENTALS


@pytest.mark.parametrize(
    "question",
    ["TCS share price", "price of reliance", "what is the stock price of infosys"],
)
def test_plain_price_questions_are_realtime(question: str) -> None:
    assert route_question(question).route is Route.REALTIME


@pytest.mark.parametrize(
    "question",
    ["what is a P/E ratio", "Explain the stock price", "what does ROE mean"],
)
def test_education_stays_general_even_if_a_word_matches_a_company(
    question: str,
) -> None:
    assert route_question(question).route is Route.GENERAL_FINANCE
    assert route_question(question, names_company=True).route is Route.GENERAL_FINANCE


def test_names_company_does_not_override_advice_or_foreign() -> None:
    assert route_question("should I buy infosys", names_company=True).route is (
        Route.ADVICE
    )
    assert route_question("apple pe ratio", names_company=True).route is (
        Route.OUT_OF_SCOPE
    )


@pytest.mark.parametrize(
    ("question", "route"),
    [
        # Owning shares is not by itself an ask for a review.
        ("I hold TCS shares, what's the current price?", Route.REALTIME),
        ("I own 100 INFY shares, what is its price to book ratio?", Route.FUNDAMENTALS),
        (
            "I have 100 TCS shares, what is the book value per share?",
            Route.FUNDAMENTALS,
        ),
        ("mere paas 10 TCS shares hain, uska PE kya hai", Route.FUNDAMENTALS),
        ("I own 300 Infosys shares. Thoughts?", Route.ADVICE),
        ("I hold TCS shares, should I book profits?", Route.ADVICE),
    ],
)
def test_owners_asking_for_data_get_data(question: str, route: Route) -> None:
    assert route_question(question).route is route


def test_a_lone_everyday_word_does_not_make_a_company_question() -> None:
    question = "How is the global economy doing?"
    assert route_question(question, names_company=True).route is Route.GENERAL_FINANCE
    assert (
        route_question(question, names_company=True, company_certain=True).route
        is Route.FUNDAMENTALS
    )
