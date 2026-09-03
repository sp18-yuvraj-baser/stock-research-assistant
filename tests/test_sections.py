"""Each case here is a real filer behaviour that broke an earlier parser."""

from sra.narrative.sections import Section, extract_blocks, split_sections


def _doc(body: str) -> bytes:
    return f"<html><body>{body}</body></html>".encode()


def _by_item(sections: list[Section]) -> dict[str, Section]:
    return {f"{s.part}:{s.item}": s for s in sections}


def test_cross_reference_inside_a_sentence_is_not_a_heading() -> None:
    # Nvidia's 10-K mentions "Item 1A. Risk Factors" mid-sentence a dozen times.
    document = _doc(
        "<div>Item 1. Business</div>"
        "<div>We design GPUs. Refer to &#8220;Item 1A. Risk Factors&#8221; for "
        "a discussion of this potential impact on our operations.</div>"
        "<div>Item 1A. Risk Factors</div>"
        "<div>Export controls could reduce demand for our products.</div>"
    )
    sections = _by_item(split_sections(document))
    assert set(sections) == {":1", ":1A"}
    assert "Export controls" in sections[":1A"].text
    assert "Refer to" in sections[":1"].text


def test_table_of_contents_is_not_mistaken_for_headings() -> None:
    toc = "".join(
        f"<tr><td>Item {n}</td><td>Title {n}</td><td>{n * 3}</td></tr>"
        for n in range(1, 9)
    )
    document = _doc(
        f"<table>{toc}</table>"
        "<div>Item 1. Business</div><div>" + ("Real business prose. " * 40) + "</div>"
        "<div>Item 1A. Risk Factors</div><div>" + ("Real risk prose. " * 40) + "</div>"
    )
    sections = split_sections(document)
    assert [s.item for s in sections] == ["1", "1A"]


def test_heading_laid_out_as_a_single_row_table_is_kept() -> None:
    # Walmart sets its real Item headings as one-row tables; stripping every
    # table deleted the headings the whole split depends on.
    document = _doc(
        "<table><tr><td>ITEM 1A.</td><td>RISK FACTORS</td></tr></table>"
        "<div>Our operations face supply chain risk.</div>"
    )
    sections = split_sections(document)
    assert [s.item for s in sections] == ["1A"]
    assert "supply chain risk" in sections[0].text


def test_financial_data_table_is_dropped() -> None:
    rows = "".join(
        f"<tr><td>Line {n}</td><td>1,{n}00</td><td>2,{n}00</td></tr>"
        for n in range(1, 8)
    )
    document = _doc(
        "<div>Item 8. Financial Statements</div>"
        f"<table>{rows}</table>"
        "<div>Narrative note that should survive.</div>"
    )
    blocks = extract_blocks(document)
    assert not any("1,100" in block for block in blocks)
    assert any("should survive" in block for block in blocks)


def test_bare_running_header_before_the_real_heading_is_collapsed() -> None:
    # Microsoft prints a bare "Item 1A" above "ITEM 1A. RISK FACTORS".
    document = _doc(
        "<div>Item 1. Business</div><div>" + ("Business prose. " * 30) + "</div>"
        "<div>Item 1A</div>"
        "<div>ITEM 1A. RISK FACTORS</div>"
        "<div>" + ("Risk prose. " * 30) + "</div>"
    )
    sections = split_sections(document)
    assert [s.item for s in sections] == ["1", "1A"]
    assert sections[1].title == "RISK FACTORS"
    assert "Risk prose" in sections[1].text


def test_ten_q_item_numbers_are_scoped_to_their_part() -> None:
    # A 10-Q restarts numbering: Part I Item 2 is MD&A, Part II Item 2 is
    # equity sales, and Risk Factors is Part II Item 1A.
    document = _doc(
        "<div>PART I. FINANCIAL INFORMATION</div>"
        "<div>Item 1. Financial Statements</div><div>Condensed statements.</div>"
        "<div>Item 2. Management's Discussion and Analysis</div>"
        "<div>Revenue grew on data center demand.</div>"
        "<div>PART II. OTHER INFORMATION</div>"
        "<div>Item 1A. Risk Factors</div><div>No material changes.</div>"
        "<div>Item 2. Unregistered Sales of Equity Securities</div>"
        "<div>We repurchased shares.</div>"
    )
    sections = _by_item(split_sections(document))
    assert set(sections) == {"I:1", "I:2", "II:1A", "II:2"}
    assert "data center demand" in sections["I:2"].text
    assert "repurchased" in sections["II:2"].text


def test_sections_do_not_bleed_into_each_other() -> None:
    document = _doc(
        "<div>Item 1A. Risk Factors</div><div>RISK_MARKER</div>"
        "<div>Item 7. Management's Discussion and Analysis</div><div>MDNA_MARKER</div>"
    )
    sections = _by_item(split_sections(document))
    assert "MDNA_MARKER" not in sections[":1A"].text
    assert "RISK_MARKER" not in sections[":7"].text


def test_document_with_no_item_headings_yields_nothing() -> None:
    assert split_sections(_doc("<div>An 8-K press release with no Items.</div>")) == []
