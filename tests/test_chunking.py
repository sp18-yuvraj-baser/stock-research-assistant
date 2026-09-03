from sra.narrative.chunking import MIN_CHARS, chunk_text


def test_short_section_is_one_chunk() -> None:
    chunks = chunk_text("A single short paragraph about export controls.")
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0


def test_chunks_respect_the_target_size() -> None:
    text = "\n".join(f"Paragraph {i}. " + ("word " * 60) for i in range(40))
    chunks = chunk_text(text, target_chars=1800, overlap_chars=200)
    assert len(chunks) > 1
    # The target is a soft bound: a chunk may exceed it by the overlap it
    # carries plus the line that tripped the boundary.
    assert all(len(c.text) <= 1800 + 200 + 400 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_consecutive_chunks_overlap() -> None:
    text = "\n".join(f"Sentence {i} about supply chain risk." for i in range(200))
    chunks = chunk_text(text, target_chars=600, overlap_chars=150)
    assert len(chunks) > 2
    # A claim split across a boundary must still be retrievable from the
    # following chunk.
    first_tail = chunks[0].text[-60:]
    assert any(word in chunks[1].text for word in first_tail.split())


def test_paragraph_longer_than_the_target_is_split_on_sentences() -> None:
    long_paragraph = " ".join(f"This is sentence number {i}." for i in range(200))
    chunks = chunk_text(long_paragraph, target_chars=500, overlap_chars=0)
    assert len(chunks) > 1
    assert all(len(c.text) <= 900 for c in chunks)


def test_trailing_fragment_is_merged_rather_than_stored_alone() -> None:
    text = "\n".join(["word " * 200, "tiny"])
    chunks = chunk_text(text, target_chars=600, overlap_chars=0)
    assert all(len(c.text) >= MIN_CHARS for c in chunks)
    assert "tiny" in chunks[-1].text


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  \n ") == []
