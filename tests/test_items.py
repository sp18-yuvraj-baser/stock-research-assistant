from sra.narrative.items import canonical_title, section_label


def test_ten_k_item_numbers_are_globally_unique() -> None:
    assert canonical_title("10-K", "I", "1A") == "Risk Factors"
    assert canonical_title("10-K", "II", "7") == "Management's Discussion and Analysis"


def test_ten_q_item_numbers_depend_on_the_part() -> None:
    assert canonical_title("10-Q", "I", "2") == "Management's Discussion and Analysis"
    assert canonical_title("10-Q", "II", "2") == (
        "Unregistered Sales of Equity Securities and Use of Proceeds"
    )


def test_amendments_share_their_parent_form_titles() -> None:
    assert canonical_title("10-K/A", "I", "1A") == "Risk Factors"


def test_statutory_title_overrides_whatever_the_filer_typed() -> None:
    # Walmart's heading table yields an empty title; Microsoft shouts its own.
    assert section_label("10-K", "I", "1A", "") == "Part I Item 1A Risk Factors"
    assert (
        section_label("10-K", "I", "1A", "RISK FACTORS")
        == "Part I Item 1A Risk Factors"
    )


def test_unknown_item_falls_back_to_the_filed_title() -> None:
    assert section_label("10-K", "I", "99", "Something Novel") == (
        "Part I Item 99 Something Novel"
    )


def test_unrecognised_form_has_no_canonical_titles() -> None:
    assert canonical_title("8-K", "I", "1") is None
