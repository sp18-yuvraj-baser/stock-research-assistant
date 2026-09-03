"""Canonical Item titles.

Filers state the same statutory heading in incompatible ways: Walmart lays
"ITEM 1A." and "RISK FACTORS" out in separate table cells, Microsoft combines
Items as "ITEM 2, 3, 4", and capitalisation varies. Citations appear in answers,
so they are normalised here rather than echoing whatever the filer typed.
"""

_TEN_K: dict[str, str] = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "[Reserved]",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "9C": "Disclosure Regarding Foreign Jurisdictions that Prevent Inspections",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners and Management",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
    "16": "Form 10-K Summary",
}

# A 10-Q reuses Item numbers across its two Parts, so these must be keyed on
# both: Part I Item 2 is MD&A, Part II Item 2 is equity sales.
_TEN_Q: dict[tuple[str, str], str] = {
    ("I", "1"): "Financial Statements",
    ("I", "2"): "Management's Discussion and Analysis",
    ("I", "3"): "Quantitative and Qualitative Disclosures About Market Risk",
    ("I", "4"): "Controls and Procedures",
    ("II", "1"): "Legal Proceedings",
    ("II", "1A"): "Risk Factors",
    ("II", "2"): "Unregistered Sales of Equity Securities and Use of Proceeds",
    ("II", "3"): "Defaults Upon Senior Securities",
    ("II", "4"): "Mine Safety Disclosures",
    ("II", "5"): "Other Information",
    ("II", "6"): "Exhibits",
}


def canonical_title(form_type: str, part: str, item: str) -> str | None:
    """The statutory title for an Item, or None if the form is not recognised."""
    family = form_type.upper().removesuffix("/A")
    if family == "10-K":
        return _TEN_K.get(item.upper())
    if family == "10-Q":
        return _TEN_Q.get((part.upper(), item.upper()))
    return None


def section_label(form_type: str, part: str, item: str, filed_title: str) -> str:
    """Citation label, preferring the statutory title over the filer's wording."""
    title = canonical_title(form_type, part, item) or filed_title.strip()
    part_prefix = f"Part {part} " if part else ""
    return f"{part_prefix}Item {item} {title}".strip()
