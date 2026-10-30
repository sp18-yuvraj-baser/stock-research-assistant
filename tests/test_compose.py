from collections.abc import Callable

import pytest

from sra.agent import compose, holding
from sra.agent.compose import OUT_OF_SCOPE, ask
from sra.agent.holding import Resolution
from sra.agent.loop import PathResult
from sra.agent.router import Route
from sra.upstox.instruments import EquityInstrument


def test_holding_question_gets_the_code_rendered_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_review(question: str) -> PathResult:
        seen.append(question)
        return PathResult(text="HDFC BANK LTD (HDFCBANK): holding review")

    monkeypatch.setattr(holding, "review", fake_review)
    question = "I have 100 hdfc bank stocks, should I keep or sell them?"
    answer = ask(question)
    assert seen == [question]
    assert answer.path is not None
    assert answer.text == "HDFC BANK LTD (HDFCBANK): holding review"


def test_foreign_market_question_is_out_of_scope_without_a_model_call() -> None:
    answer = ask("What is Apple's P/E ratio?")
    assert answer.text == OUT_OF_SCOPE
    assert answer.paths == []


def _recording_path(ran: list[str], route: Route) -> Callable[..., PathResult]:
    def path(question: str, **_: object) -> PathResult:
        ran.append(route.value)
        return PathResult(text=route.value)

    return path


def _stub_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []
    for route in (Route.REALTIME, Route.FUNDAMENTALS, Route.GENERAL_FINANCE):
        monkeypatch.setitem(
            compose._MODEL_PATHS,
            route,
            _recording_path(ran, route),
        )
    return ran


def _resolves(
    monkeypatch: pytest.MonkeyPatch, *companies: EquityInstrument
) -> list[str]:
    looked_up: list[str] = []

    def fake(question: str) -> Resolution:
        looked_up.append(question)
        return Resolution(list(companies), [])

    monkeypatch.setattr(holding, "resolve_companies", fake)
    return looked_up


def test_company_named_in_plain_words_gets_the_fundamentals_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _stub_paths(monkeypatch)
    _resolves(
        monkeypatch,
        EquityInstrument("INFY", "NSE_EQ|INE009A01021", "NSE_EQ", "INFOSYS LTD"),
    )
    answer = ask("what is the pe of infosys")
    assert answer.route is Route.FUNDAMENTALS
    assert ran == ["fundamentals"]


def test_education_question_stays_general_and_costs_one_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _stub_paths(monkeypatch)
    looked_up = _resolves(monkeypatch)
    assert ask("what is a P/E ratio").route is Route.GENERAL_FINANCE
    assert ran == ["general_finance"]
    assert len(looked_up) == 1


def test_no_lookup_when_the_regexes_already_route_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _stub_paths(monkeypatch)
    looked_up = _resolves(
        monkeypatch, EquityInstrument("TCS", "NSE_EQ|INE467B01029", "NSE_EQ", "TCS LTD")
    )
    assert ask("What is HDFC Bank's P/E ratio?").route is Route.FUNDAMENTALS
    assert ask("TCS share price").route is Route.REALTIME
    assert looked_up == []
    assert ran == ["fundamentals", "realtime"]


def test_a_request_for_stocks_gets_the_code_rendered_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sra.agent import screen

    monkeypatch.setattr(screen, "run", lambda q: PathResult(text="screened"))
    answer = ask("suggest me some stocks for long term investment")
    assert answer.route is Route.SCREEN
    assert answer.text == "screened"
