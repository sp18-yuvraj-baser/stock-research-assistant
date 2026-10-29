from typing import Any

import pytest

from sra.agent import compose, followup, holding
from sra.agent.followup import Turn, standalone_question
from sra.agent.loop import ModelUnavailableError, PathResult

HISTORY = [
    Turn("user", "I have 100 hdfc bank stocks, should I keep or sell them?"),
    Turn("assistant", "HDFC BANK LTD (HDFCBANK): review ..."),
]


def _model_says(monkeypatch: pytest.MonkeyPatch, content: str) -> list[str]:
    prompts: list[str] = []

    def fake_chat(
        client: object, messages: list[dict[str, Any]], tools: list[Any], **_: Any
    ) -> dict[str, Any]:
        prompts.append(messages[-1]["content"])
        return {"message": {"content": content}}

    monkeypatch.setattr(followup, "chat", fake_chat)
    return prompts


def test_follow_up_is_rewritten_with_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _model_says(
        monkeypatch,
        'Standalone question: "I have 100 HDFC Bank shares, should I keep or sell '
        'them?"',
    )
    rewrite = standalone_question("reply to above question", HISTORY)
    assert rewrite == "I have 100 HDFC Bank shares, should I keep or sell them?"
    assert "User: I have 100 hdfc bank stocks" in prompts[0]
    assert "Latest message: reply to above question" in prompts[0]


def test_a_message_that_does_not_refer_back_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _model_says(monkeypatch, "What is ROE for HDFC Bank?")
    assert standalone_question("What is ROE?", HISTORY) == "What is ROE?"
    assert prompts == []


def test_no_earlier_question_means_no_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _model_says(monkeypatch, "anything")
    history = [Turn("assistant", "Hi, what would you like to research?")]
    assert standalone_question("and Infosys?", history) == "and Infosys?"
    assert prompts == []


@pytest.mark.parametrize("content", ["", "x" * 500])
def test_an_untrustworthy_rewrite_falls_back_to_the_message(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _model_says(monkeypatch, content)
    assert standalone_question("and Infosys?", HISTORY) == "and Infosys?"


def test_model_down_falls_back_to_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def down(*args: object, **kwargs: object) -> dict[str, Any]:
        raise ModelUnavailableError("down")

    monkeypatch.setattr(followup, "chat", down)
    assert standalone_question("and Infosys?", HISTORY) == "and Infosys?"


def test_ask_routes_and_answers_the_rewritten_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rewrite = "I have 100 HDFC Bank shares, should I keep or sell them?"
    monkeypatch.setattr(compose, "standalone_question", lambda q, h: rewrite)
    reviewed: list[str] = []

    def fake_review(question: str) -> PathResult:
        reviewed.append(question)
        return PathResult(text="review")

    monkeypatch.setattr(holding, "review", fake_review)
    answer = compose.ask("reply to above question", history=HISTORY)
    assert reviewed == [rewrite]
    assert answer.question == "reply to above question"
    assert answer.standalone == rewrite
    assert answer.route.value == "advice"
