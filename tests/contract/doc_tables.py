"""The exit-code tables in the recovery docs, checked against the
contract's codes (#888).

An heir reads an exit code in a table: RESTORE.txt, RESTORE_WINDOWS.txt,
TIERS.txt, restore.sh's header.  Those tables are written by hand for
their reader and are not generated (a generated block in a burned doc
needs markers and a build step, and each table words the codes for its
own audience).  So they are checked instead: each table in
:data:`TABLES` must list exactly the codes its program has (the enums
and key words of ``exit_codes.py``), each once, and each row must
contain one of its code's key words.

A table is the rows after a heading line (the first line that contains
:attr:`Table.heading`; it must be the only one).  The rest of the
heading's paragraph and blank lines may come between the heading and
the first row.  A row is a line at the first
row's indent that starts with a number, or numbers joined by "," or
"/"; ``--`` may follow them.  Deeper lines continue the row above.  The
table ends at a blank line or at a line, not deeper than the rows, that
is not a row.  A Markdown table's rows are ``| <number> | <text> |``;
its other rows (the header, the rule, "other") are passed over.

``style`` says how a source line becomes a table line: ``"text"`` as it
is, ``"sh"`` without its leading ``#``, ``"c"`` the text of a C string
literal (any other line is blank there).

The test is ``tests/recovery_hardening/test_doc_exit_tables.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tests.contract.exit_codes import (
    CHECK_DISC_WORDS,
    ECC_WORDS,
    READER_WORDS,
    SCRIPT_WORDS,
    STOP_WORDS,
    StopExit,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Program:
    """A program whose exit codes a table lists: each code and its key
    words.  The keys are the codes a table must list, no more."""

    name: str
    words: dict[int, tuple[str, ...]]


TIER1 = Program("tier 1 (lcsas-restore)", READER_WORDS)
TIER3 = Program("tier 3 (the Python reader)",
                {**READER_WORDS,
                 StopExit.INTERRUPT: STOP_WORDS[StopExit.INTERRUPT]})
RESTORE_SH = Program("restore.sh", {**SCRIPT_WORDS, **STOP_WORDS})
RESTORE_BAT = Program("restore.bat", SCRIPT_WORDS)
CHECK_DISC = Program("restore.sh --check-disc", CHECK_DISC_WORDS)
LCSAS_ECC = Program("lcsas-ecc", ECC_WORDS)


@dataclass(frozen=True)
class Table:
    path: str                # relative to the repository root
    heading: str             # text of the line the table follows
    program: Program
    style: str = "text"      # "text", "sh" or "c"

    def where(self) -> str:
        return f"{self.path}, under [{self.heading}] ({self.program.name})"


TABLES: tuple[Table, ...] = (
    Table("recovery/docs/RESTORE.txt", "lcsas-restore exits:", TIER1),
    Table("recovery/docs/RESTORE.txt", "restore.sh exits:", RESTORE_SH),
    Table("recovery/docs/RESTORE_WINDOWS.txt", "EXIT CODES of restore.bat",
          RESTORE_BAT),
    Table("recovery/docs/TIERS.txt", "Tier 1 exit status, in full:", TIER1),
    Table("recovery/docs/TIERS.txt",
          "Tier 3 (python_reader.py) exit status:", TIER3),
    Table("recovery/scripts/restore.sh", "Documented exit codes", RESTORE_SH,
          style="sh"),
    Table("recovery/scripts/restore.sh", "Exit codes, from the wrapped tool's:",
          CHECK_DISC, style="sh"),
    # What `lcsas-restore --help` prints.
    Table("recovery/src/lcsas-restore/main.c", '"Exit status:\\n"', TIER1,
          style="c"),
    Table("docs/workflows/restore-windows.md", "## Exit codes", RESTORE_BAT),
    Table("docs/DVDISASTER_RS03_FORMAT.md", "`lcsas-ecc` exit codes",
          LCSAS_ECC),
)


@dataclass(frozen=True)
class Row:
    codes: tuple[int, ...]
    text: str                # the row and its continuation lines, one line
    line: int                # 1-based, in the file


_ROW = re.compile(r"(\s*)(\d+(?:\s*[,/]\s*\d+)*)(?:\s+--)?(?:\s+(.*))?$")
_PIPE_ROW = re.compile(r"\|\s*`?([^|`]*?)`?\s*\|(.*)\|\s*$")
_C_STRING = re.compile(r'\s*"(.*?)(?:\\n)?"\s*[,;)]*\s*$')


def _line(raw: str, style: str) -> str:
    if style == "sh":
        return raw[1:] if raw.startswith("#") else ""
    if style == "c":
        m = _C_STRING.match(raw)
        return m.group(1) if m else ""
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


class TableError(Exception):
    pass


def parse(table: Table, text: str) -> list[Row]:
    """The rows of ``table`` in ``text`` (the file's content)."""
    raw = text.splitlines()
    starts = [i for i, s in enumerate(raw) if table.heading in s]
    if len(starts) != 1:
        raise TableError(f"{table.where()}: {len(starts)} heading lines, "
                         f"not 1")
    i = starts[0] + 1
    # The rest of the heading's paragraph, then blank lines.
    while i < len(raw) and _line(raw[i], table.style).strip() and not (
            _ROW.match(_line(raw[i], table.style))
            or _line(raw[i], table.style).lstrip().startswith("|")):
        i += 1
    while i < len(raw) and not _line(raw[i], table.style).strip():
        i += 1
    rows: list[tuple[tuple[int, ...], list[str], int]] = []
    pipe = i < len(raw) and _line(raw[i], table.style).lstrip().startswith("|")
    indent = _indent(_line(raw[i], table.style)) if i < len(raw) else 0
    for n in range(i, len(raw)):
        line = _line(raw[n], table.style)
        if not line.strip():
            break
        if pipe:
            m = _PIPE_ROW.match(line.strip())
            if not m:
                break
            if m.group(1).isdigit():
                rows.append(((int(m.group(1)),), [m.group(2)], n + 1))
            continue
        m = _ROW.match(line)
        if m and len(m.group(1)) == indent:
            codes = tuple(int(c) for c in re.split(r"[,/]", m.group(2)))
            rows.append((codes, [m.group(3) or ""], n + 1))
        elif _indent(line) > indent and rows:
            rows[-1][1].append(line)
        else:
            break
    if not rows:
        raise TableError(f"{table.where()}: no rows after the heading")
    return [Row(codes, " ".join(" ".join(parts).split()), line)
            for codes, parts, line in rows]


def problems(table: Table, text: str | None = None) -> list[str]:
    """What is wrong with ``table``; ``text`` stands in for the file."""
    if text is None:
        text = (REPO_ROOT / table.path).read_text(encoding="utf-8")
    try:
        rows = parse(table, text)
    except TableError as exc:
        return [str(exc)]
    out: list[str] = []
    seen: dict[int, int] = {}
    for row in rows:
        for code in row.codes:
            if code in seen:
                out.append(f"{table.where()}: {code} is listed twice "
                           f"(lines {seen[code]} and {row.line})")
            seen[code] = row.line
            words = table.program.words.get(code)
            if words is None:
                out.append(f"{table.where()}: line {row.line} lists {code}, "
                           f"which {table.program.name} does not exit with")
            elif not any(w.lower() in row.text.lower() for w in words):
                out.append(f"{table.where()}: line {row.line}: the row for "
                           f"{code} says none of {list(words)}: {row.text!r}")
    for code in sorted(set(table.program.words) - set(seen)):
        out.append(f"{table.where()}: {code} is not listed")
    return out


__all__ = ["CHECK_DISC", "LCSAS_ECC", "RESTORE_BAT", "RESTORE_SH", "TABLES",
           "TIER1", "TIER3", "Program", "Row", "Table", "TableError",
           "parse", "problems"]
