import re
from dataclasses import dataclass

from lxml.html import HtmlElement, fromstring

# Blocks that carry a visual line break. Inline XBRL filings nest these many
# levels deep, so only the innermost one is read; otherwise every ancestor
# repeats its descendants' text.
_BLOCK_TAGS = frozenset(
    {"div", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dd", "dt"}
)

# An Item heading occupies a whole block on its own. The same string appears
# many times inside sentences as a cross-reference ("Refer to Item 1A. Risk
# Factors for..."), which is why anchoring to the whole block matters more than
# any font or style rule -- styling differs for every filer.
_HEADING = re.compile(
    r"^item\s*(\d{1,2})\s*([a-c])?\s*[.:)\u2014\u2013-]*\s*(.{0,200})$",
    re.IGNORECASE,
)

# A 10-Q restarts Item numbering in each Part: MD&A is Part I Item 2 while Risk
# Factors is Part II Item 1A. Without tracking Parts the two sequences collide.
_PART = re.compile(
    r"^part\s+(i{1,3}v?|iv)\b[\s.:\u2014\u2013-]*(.{0,80})$", re.IGNORECASE
)
_PART_RANK = {"I": 1, "II": 2, "III": 3, "IV": 4}

_DIGITS = re.compile(r"\d")

# Below this a table is layout scaffolding, not tabular content. Walmart sets
# its real Item headings as one-row tables, so stripping every table would
# delete the headings the whole split depends on.
_LAYOUT_TABLE_MAX_CELLS = 3
_DATA_TABLE_MIN_CELLS = 12
_DATA_TABLE_NUMERIC_SHARE = 0.3


@dataclass(frozen=True)
class Section:
    part: str  # 'I', 'II', or '' when the filing states no Part
    item: str  # '1A', '7', '7A'
    title: str  # 'Risk Factors'
    text: str

    @property
    def label(self) -> str:
        """Citation label, e.g. 'Part II Item 1A Risk Factors'."""
        part = f"Part {self.part} " if self.part else ""
        return f"{part}Item {self.item} {self.title}".strip()


@dataclass(frozen=True)
class _Candidate:
    line_index: int
    part: str
    item: str
    title: str
    order: tuple[int, int]


def _select(root: HtmlElement, path: str) -> list[HtmlElement]:
    """xpath() is typed as returning strings and numbers too; keep only nodes."""
    return [node for node in root.xpath(path) if isinstance(node, HtmlElement)]


def _is_data_table(table: HtmlElement) -> bool:
    """Distinguish a financial table from a table used purely for layout.

    Table-heavy content is deliberately out of scope for the narrative path, and
    the table of contents would otherwise be mistaken for real headings. But
    some filers wrap headings in tables, so the two cases have to be told apart
    by shape rather than by tag.
    """
    cells = _select(table, ".//td | .//th")
    if len(cells) <= _LAYOUT_TABLE_MAX_CELLS:
        return False
    if len(cells) > _DATA_TABLE_MIN_CELLS:
        return True
    numeric = sum(1 for cell in cells if _DIGITS.search(cell.text_content() or ""))
    return numeric / len(cells) > _DATA_TABLE_NUMERIC_SHARE


def extract_blocks(document: bytes) -> list[str]:
    """Flatten a filing's HTML into visual lines, dropping tabular content."""
    tree = fromstring(document)
    for element in _select(tree, "//script | //style"):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    # Innermost first, so a nested data table is judged on its own shape rather
    # than inflating its wrapper's cell count.
    for table in reversed(_select(tree, "//table")):
        if _is_data_table(table):
            parent = table.getparent()
            if parent is not None:
                parent.remove(table)

    lines: list[str] = []
    for element in tree.iter():
        if not isinstance(element, HtmlElement):
            continue
        if not isinstance(element.tag, str) or element.tag not in _BLOCK_TAGS:
            continue
        if any(
            isinstance(child.tag, str) and child.tag in _BLOCK_TAGS
            for child in element.iterdescendants()
        ):
            continue
        text = " ".join(element.text_content().split())
        if text:
            lines.append(text)
    return lines


def _item_rank(item_number: int, suffix: str) -> int:
    """Sortable rank so 1 < 1A < 1B < 2 and 9C < 10."""
    return item_number * 10 + (ord(suffix) - 64 if suffix else 0)


def _candidates(lines: list[str]) -> list[_Candidate]:
    found: list[_Candidate] = []
    part = ""
    part_rank = 0
    for index, line in enumerate(lines):
        part_match = _PART.match(line)
        if part_match:
            candidate_part = part_match.group(1).upper()
            if candidate_part in _PART_RANK:
                part = candidate_part
                part_rank = _PART_RANK[candidate_part]
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        number = int(match.group(1))
        suffix = (match.group(2) or "").upper()
        found.append(
            _Candidate(
                line_index=index,
                part=part,
                item=f"{number}{suffix}",
                title=match.group(3).strip(" .:-\u2014\u2013"),
                order=(part_rank, _item_rank(number, suffix)),
            )
        )
    return found


def _dedupe_repeats(candidates: list[_Candidate]) -> list[_Candidate]:
    """Collapse a running header immediately followed by the real heading.

    Microsoft prints a bare 'Item 1A' above 'ITEM 1A. RISK FACTORS'. Both match,
    and the untitled one would otherwise break the ascending sequence.
    """
    kept: list[_Candidate] = []
    for candidate in candidates:
        if kept and kept[-1].order == candidate.order:
            # Prefer whichever states a title; that is the real heading.
            if not kept[-1].title and candidate.title:
                kept[-1] = candidate
            continue
        kept.append(candidate)
    return kept


def _longest_ascending_run(
    candidates: list[_Candidate], lines: list[str]
) -> list[_Candidate]:
    """Pick the body's heading sequence and discard the table of contents.

    A filing lists its Items twice: once in the contents and once as real
    headings, each ascending. Both runs ascend, so ordering alone cannot
    separate them -- the run covering the most text is the body.
    """
    if not candidates:
        return []
    runs: list[list[_Candidate]] = [[candidates[0]]]
    for candidate in candidates[1:]:
        if candidate.order > runs[-1][-1].order:
            runs[-1].append(candidate)
        else:
            runs.append([candidate])

    def span(run: list[_Candidate]) -> int:
        return sum(
            len(line) for line in lines[run[0].line_index : run[-1].line_index + 1]
        )

    return max(runs, key=span)


def split_sections(document: bytes) -> list[Section]:
    """Split a filing into Item-level sections.

    Item boundaries are the meaningful ones, so they are found first and
    chunking happens inside them; a fixed window would merge the end of Risk
    Factors into the start of MD&A.
    """
    lines = extract_blocks(document)
    headings = _longest_ascending_run(_dedupe_repeats(_candidates(lines)), lines)
    sections: list[Section] = []
    for position, heading in enumerate(headings):
        start = heading.line_index + 1
        end = (
            headings[position + 1].line_index
            if position + 1 < len(headings)
            else len(lines)
        )
        body = [line for line in lines[start:end] if not _PART.match(line)]
        text = "\n".join(body).strip()
        if not text:
            continue
        sections.append(
            Section(
                part=heading.part,
                item=heading.item,
                title=heading.title,
                text=text,
            )
        )
    return sections
