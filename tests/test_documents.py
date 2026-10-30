from datetime import UTC, datetime
from typing import Any

import pytest

from sra.agent import documents
from sra.agent.companies import Resolution
from sra.agent.documents import Filing, best_passages, passages, quotes_check
from sra.agent.loop import ModelUnavailableError
from sra.upstox.instruments import EquityInstrument

TEXT = """Revenue grew 12% in the quarter on strong demand in North America.

Attrition fell to 13% as hiring normalised across delivery centres.

The board declared an interim dividend of Rs 12 per share.

Deal wins were USD 8 billion, the highest in six quarters."""


def test_passages_split_on_paragraphs_and_cap_size() -> None:
    chunks = passages(TEXT + "\n\n" + "x " * 2000)
    assert chunks[0].startswith("Revenue grew 12%")
    assert all(len(c) <= documents.PASSAGE_CHARS * 2 for c in chunks)


def test_best_passages_match_the_question_and_keep_document_order() -> None:
    chunks = [" ".join(p.split()) for p in TEXT.split("\n\n")]
    assert best_passages("what did they say about attrition", chunks) == [chunks[1]]
    summary = best_passages("summarise the earnings call", chunks)
    assert summary[0] == chunks[0] and chunks[3] in summary  # revenue, deal terms


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        ('Attrition "fell to 13% as hiring normalised" [2].', True),
        ('Attrition "fell to 9%" [2].', False),  # not in the passages
        ("Attrition fell.", False),  # no quotation at all
    ],
)
def test_quotes_check(answer: str, ok: bool) -> None:
    assert quotes_check(answer, [" ".join(p.split()) for p in TEXT.split("\n\n")]) is ok


@pytest.fixture
def filing(monkeypatch: pytest.MonkeyPatch) -> None:
    tcs = EquityInstrument("TCS", "NSE_EQ|INE467B01029", "NSE_EQ", "TATA CONSULTANCY")
    monkeypatch.setattr(documents, "resolve_companies", lambda q: Resolution([tcs], []))
    monkeypatch.setattr(
        documents,
        "_latest_filing",
        lambda isin, categories: Filing(
            "TCS", "Transcript of Q2 call", "https://bse.example/t.pdf",
            datetime(2026, 10, 20, 10, 0, tzinfo=UTC),
        ),
    )  # fmt: skip
    monkeypatch.setattr(documents, "pdf_text", lambda url: TEXT)


def _model_says(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    def fake_chat(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"message": {"content": content}}

    monkeypatch.setattr(documents, "chat", fake_chat)


def test_verified_quotes_are_the_answer(
    filing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, 'Attrition "fell to 13% as hiring normalised" [1].')
    text = documents.answer("what did TCS say about attrition on the call").text
    assert "Answer, quoted from the filing" in text
    assert "filed 20 Oct 2026" in text and "https://bse.example/t.pdf" in text


def test_an_unverifiable_quote_falls_back_to_passages(
    filing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, 'Attrition "dropped sharply to 9%" [1].')
    text = documents.answer("what did TCS say about attrition on the call").text
    assert "Relevant passages from the filing" in text
    assert "dropped sharply" not in text


def test_no_model_still_answers_with_passages(
    filing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ModelUnavailableError("down")

    monkeypatch.setattr(documents, "chat", down)
    text = documents.answer("what did TCS say about attrition on the call").text
    assert "Attrition fell to 13%" in text


def test_only_https_filings_are_fetched() -> None:
    with pytest.raises(ValueError, match="https"):
        documents.pdf_text("http://bse.example/t.pdf")
