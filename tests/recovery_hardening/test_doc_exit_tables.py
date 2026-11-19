"""The exit-code tables in the recovery docs list the codes the
programs exit with, each under its own meaning (#888).

The tables an heir reads were the least protected copy of the exit
codes: a wrong number in RECOVER.txt passed every gate.  Each table in
``tests/contract/doc_tables.py`` must list exactly its program's codes
(``tests/contract/exit_codes.py``), each once, and each row must hold
one of its code's key words.  The docs are checked, not generated: see
``doc_tables.py``.
"""
from __future__ import annotations

import dataclasses
import re

import pytest

from tests.contract import doc_tables
from tests.contract.doc_tables import REPO_ROOT, TABLES, Program, Table
from tests.contract.exit_codes import (
    CHECK_DISC_EXITS,
    CHECK_DISC_WORDS,
    ECC_WORDS,
    READER_WORDS,
    SCRIPT_WORDS,
    STOP_WORDS,
    EccExit,
    ReaderExit,
    ScriptExit,
    StopExit,
)


def _id(table: Table) -> str:
    return f"{table.path}:{table.heading}"


def _text(table: Table) -> str:
    return (REPO_ROOT / table.path).read_text(encoding="utf-8")


@pytest.mark.parametrize("table", TABLES, ids=_id)
def test_table_lists_the_programs_codes(table: Table) -> None:
    assert doc_tables.problems(table) == []


def test_every_code_has_key_words() -> None:
    """A code added to an enum without key words fails here, so it
    cannot be left out of the tables unnoticed."""
    assert set(READER_WORDS) == set(ReaderExit)
    assert set(SCRIPT_WORDS) == set(ScriptExit)
    assert set(CHECK_DISC_WORDS) == CHECK_DISC_EXITS
    assert set(ECC_WORDS) == set(EccExit)
    assert set(STOP_WORDS) == set(StopExit)
    for words in (READER_WORDS, SCRIPT_WORDS, CHECK_DISC_WORDS, ECC_WORDS,
                  STOP_WORDS):
        assert all(words.values())


def test_every_program_has_a_table() -> None:
    programs = {t.program.name for t in TABLES}
    for program in (doc_tables.TIER1, doc_tables.TIER3, doc_tables.RESTORE_SH,
                    doc_tables.RESTORE_BAT, doc_tables.CHECK_DISC,
                    doc_tables.LCSAS_ECC):
        assert program.name in programs


# ── The check fails when it should ──────────────────────────────────


def _says_where(found: list[str], table: Table) -> None:
    assert found, "the change went unnoticed"
    assert all(table.path in p and table.heading in p for p in found), found


@pytest.mark.parametrize("table", TABLES, ids=_id)
def test_a_removed_row_fails(table: Table) -> None:
    text = _text(table)
    lines = text.splitlines()
    for row in doc_tables.parse(table, text):
        cut = "\n".join(lines[:row.line - 1] + lines[row.line:])
        _says_where(doc_tables.problems(table, cut), table)


@pytest.mark.parametrize("table", TABLES, ids=_id)
def test_a_changed_number_fails(table: Table) -> None:
    text = _text(table)
    lines = text.splitlines()
    for row in doc_tables.parse(table, text):
        line = lines[row.line - 1]
        changed = re.sub(rf"\b{row.codes[0]}\b", "99", line, count=1)
        assert changed != line
        new = "\n".join(lines[:row.line - 1] + [changed] + lines[row.line:])
        _says_where(doc_tables.problems(table, new), table)


@pytest.mark.parametrize("table", TABLES, ids=_id)
def test_a_code_the_doc_lacks_fails(table: Table) -> None:
    program = Program(table.program.name,
                      {**table.program.words, 99: ("anything",)})
    found = doc_tables.problems(dataclasses.replace(table, program=program))
    assert any("99 is not listed" in p for p in found), found
    _says_where(found, table)


@pytest.mark.parametrize("table", TABLES, ids=_id)
def test_a_row_under_the_wrong_meaning_fails(table: Table) -> None:
    """Swapping two rows' numbers keeps the set: the key words catch it."""
    text = _text(table)
    rows = doc_tables.parse(table, text)
    for a, b in zip(rows, rows[1:], strict=False):
        lines = text.splitlines()
        swap = {a.codes[0]: b.codes[0], b.codes[0]: a.codes[0]}
        for row in (a, b):
            lines[row.line - 1] = re.sub(rf"\b{row.codes[0]}\b",
                                         str(swap[row.codes[0]]),
                                         lines[row.line - 1], count=1)
        _says_where(doc_tables.problems(table, "\n".join(lines)), table)
