"""Read and write the markdown files Jef keeps its state in.

Each file is some free text (a title, notes) followed by one pipe table.
We keep the text before and after the table exactly as written, and only
rewrite the table itself, so people can add their own notes to the files.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TableDoc:
    """A markdown document holding one pipe table."""

    before: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


def _is_table_line(line: str) -> bool:
    return line.strip().startswith("|")


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        c and set(c.replace(":", "")) <= {"-"} and "-" in c for c in cells
    )


def split_row(line: str) -> list[str]:
    """Split one table line into cells, honouring escaped pipes (\\|)."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current).strip())
    return cells


def escape_cell(value: str) -> str:
    """Make a value safe to put in a table cell."""
    return " ".join(value.split()).replace("|", "\\|")


def parse(text: str) -> TableDoc:
    """Parse a document. A document with no table gives empty headers and rows."""
    lines = text.splitlines()
    doc = TableDoc()
    i = 0
    while i < len(lines):
        if (
            _is_table_line(lines[i])
            and i + 1 < len(lines)
            and _is_table_line(lines[i + 1])
            and _is_separator(split_row(lines[i + 1]))
        ):
            break
        i += 1
    if i >= len(lines):
        doc.before = lines
        return doc

    doc.before = lines[:i]
    doc.headers = split_row(lines[i])
    i += 2
    while i < len(lines) and _is_table_line(lines[i]):
        cells = split_row(lines[i])
        # Pad or trim so every row matches the header width.
        cells = (cells + [""] * len(doc.headers))[: len(doc.headers)]
        doc.rows.append(cells)
        i += 1
    doc.after = lines[i:]
    return doc


def _row_line(cells: list[str]) -> str:
    # An empty cell renders as "| |", a filled one as "| value |".
    return "|" + "|".join(f" {c} " if c else " " for c in map(escape_cell, cells)) + "|"


def render(doc: TableDoc) -> str:
    """Turn a document back into markdown text."""
    out = list(doc.before)
    if doc.headers:
        out.append(_row_line(doc.headers))
        out.append(_row_line(["---"] * len(doc.headers)))
        for row in doc.rows:
            out.append(_row_line(row))
    out.extend(doc.after)
    return "\n".join(out) + "\n"
