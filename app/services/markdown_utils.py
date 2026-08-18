"""Shared Markdown parsing helpers.

Both the DOCX exporter and the validation gate need to read GFM tables and
section boundaries out of generated Markdown. Keeping one parser here means the
validator sees exactly the same table the exporter will render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

TABLE_DIVIDER_PATTERN = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_PATTERN = re.compile(r"^\s*```")
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")


@dataclass
class MarkdownTable:
    """A parsed GFM table."""

    header: List[str]
    rows: List[List[str]] = field(default_factory=list)
    line_number: int = 0

    @property
    def title(self) -> str:
        """First header cell, used to identify the table's purpose."""
        return self.header[0] if self.header else ""

    def column(self, index: int) -> List[str]:
        return [row[index] if index < len(row) else "" for row in self.rows]


@dataclass
class MarkdownSection:
    """A `##`-level (or deeper) section and its body text."""

    level: int
    title: str
    body: str
    line_number: int = 0


def split_row(line: str) -> List[str]:
    """Split a Markdown table row into trimmed cell values."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def is_table_start(lines: List[str], index: int) -> bool:
    """True when `lines[index]` is a header row followed by a divider row."""
    if index >= len(lines) or "|" not in lines[index]:
        return False
    if index + 1 >= len(lines):
        return False
    return bool(TABLE_DIVIDER_PATTERN.match(lines[index + 1]))


def consume_table(lines: List[str], index: int) -> tuple[List[List[str]], int]:
    """Collect the table starting at `index`; returns (rows, next_index)."""
    header = split_row(lines[index])
    cursor = index + 2  # skip header and divider
    rows: List[List[str]] = [header]
    while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
        cells = split_row(lines[cursor])
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(cells[: len(header)])
        cursor += 1
    return rows, cursor


def parse_tables(markdown: str) -> List[MarkdownTable]:
    """Extract every GFM table in `markdown`, ignoring fenced code blocks."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    tables: List[MarkdownTable] = []
    index = 0
    in_fence = False
    while index < len(lines):
        if FENCE_PATTERN.match(lines[index]):
            in_fence = not in_fence
            index += 1
            continue
        if not in_fence and is_table_start(lines, index):
            rows, next_index = consume_table(lines, index)
            tables.append(
                MarkdownTable(header=rows[0], rows=rows[1:], line_number=index + 1)
            )
            index = next_index
            continue
        index += 1
    return tables


def parse_sections(markdown: str, level: int = 2) -> List[MarkdownSection]:
    """Split `markdown` into sections at the given heading level.

    Headings inside fenced code blocks are ignored so a `# comment` line in a
    shell example never splits the document.
    """
    lines = markdown.replace("\r\n", "\n").split("\n")
    sections: List[MarkdownSection] = []
    current: Optional[MarkdownSection] = None
    buffer: List[str] = []
    in_fence = False

    for number, line in enumerate(lines, start=1):
        if FENCE_PATTERN.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue

        match = None if in_fence else HEADING_PATTERN.match(line)
        if match and len(match.group(1)) == level:
            if current is not None:
                current.body = "\n".join(buffer).strip()
                sections.append(current)
            current = MarkdownSection(
                level=level, title=match.group(2).strip(), body="", line_number=number
            )
            buffer = []
            continue
        buffer.append(line)

    if current is not None:
        current.body = "\n".join(buffer).strip()
        sections.append(current)
    return sections


def section_map(markdown: str, level: int = 2) -> Dict[str, MarkdownSection]:
    """Sections keyed by lowercased title, for keyword lookup."""
    return {section.title.lower(): section for section in parse_sections(markdown, level)}


def find_section(markdown: str, *keywords: str, level: int = 2) -> Optional[MarkdownSection]:
    """First section whose title contains every keyword (case-insensitive)."""
    for section in parse_sections(markdown, level):
        title = section.title.lower()
        if all(keyword.lower() in title for keyword in keywords):
            return section
    return None


def has_code_evidence(text: str) -> bool:
    """True when `text` contains a fenced block or an inline code span.

    Used to prove an execution step names a real command rather than gesturing
    at one ("verify access").
    """
    return "```" in text or bool(INLINE_CODE_PATTERN.search(text))


def code_blocks(text: str) -> List[str]:
    """Return the contents of every fenced code block in `text`."""
    blocks: List[str] = []
    buffer: List[str] = []
    in_fence = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if FENCE_PATTERN.match(line):
            if in_fence:
                blocks.append("\n".join(buffer))
                buffer = []
            in_fence = not in_fence
            continue
        if in_fence:
            buffer.append(line)
    if in_fence and buffer:  # unterminated fence
        blocks.append("\n".join(buffer))
    return blocks


def normalise(text: str) -> str:
    """Lowercase and collapse whitespace, for tolerant name/keyword matching."""
    return re.sub(r"\s+", " ", text).strip().lower()
