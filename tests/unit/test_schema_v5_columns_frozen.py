"""FMT-05, #961: pin the frozen catalog surface the heir-path readers read.

The tier-1 C reader (recovery/src/lcsas-restore/catalog.c) is burned onto
every META disc and can never be patched after the fact; the Python
reader (tier 3, the generated ``python_reader.py``) is burned beside it.
Both read every disc catalog with SQL fixed when the disc was made.
``TIER1_FROZEN_SURFACE`` (and the pragmas beside it) in
``lcsas.db.schema`` is the contract that keeps a later schema from
breaking them.

#961: the pin is taken from the readers, not written beside them.  Every
SQL statement in catalog.c (adjacent C string literals joined) and every
statement the Python reader runs is prepared against a catalog made by
``create_all()``; SQLite's authorizer reports each table and column it
reads, and each one must be on the frozen surface.  Each statement must
also prepare against a catalog holding the frozen surface and nothing
else.  A new query, or a column added to one, fails here until the
surface is widened on purpose.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from lcsas.db.schema import (
    CATALOG_APPLICATION_ID,
    TIER1_FROZEN_PRAGMAS,
    TIER1_FROZEN_SURFACE,
    TIER1_LABEL_MARK_PRAGMA,
    TIER1_REQUIRED_VOLUME_STATUS,
    create_all,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG_C = ROOT / "recovery" / "src" / "lcsas-restore" / "catalog.c"
FORMAT_TXT = ROOT / "recovery" / "docs" / "FORMAT.txt"

_SQL_START = re.compile(r"\s*(SELECT|PRAGMA|WITH|INSERT|UPDATE|DELETE|"
                        r"REPLACE|CREATE|DROP|ALTER|ATTACH)\b", re.I)


def _fresh_catalog() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all(conn)
    return conn


# ── The SQL of the readers ──────────────────────────────────────────


def _c_tokens(text: str) -> tuple[str, list[str]]:
    """catalog.c with comments dropped and each run of adjacent string
    literals replaced by ``@N@``; returns (that text, the joined
    literals).  C89 has no raw strings; the escapes catalog.c uses in
    SQL are none, so a literal's value is its text between the quotes
    with ``\\"`` and ``\\\\`` undone."""
    out: list[str] = []
    literals: list[str] = []
    pending: list[str] | None = None   # literals joined so far
    i = 0
    n = len(text)

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            out.append(f"@{len(literals)}@")
            literals.append("".join(pending))
            pending = None

    while i < n:
        ch = text[i]
        if text.startswith("/*", i):
            end = text.index("*/", i + 2)
            i = end + 2
            out.append(" ")
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == '"':
            j = i + 1
            buf = []
            while text[j] != '"':
                if text[j] == "\\":
                    buf.append(text[j:j + 2])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            value = "".join(buf).replace('\\"', '"').replace("\\\\", "\\")
            pending = [value] if pending is None else [*pending, value]
            i = j + 1
            continue
        if ch == "'":
            j = text.index("'", i + 1 + (text[i + 1] == "\\"))
            flush()
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if ch.isspace() and pending is not None:
            i += 1
            continue
        flush()
        out.append(ch)
        i += 1
    flush()
    return "".join(out), literals


def c_reader_sql() -> list[str]:
    """Every SQL statement tier 1 runs on a catalog, from catalog.c.

    Each ``sqlite3_prepare_v2`` and ``query_int`` call must take a
    string literal (``query_int``'s own prepare of its ``sql`` argument
    is the one exception), and every literal that reads as SQL must be
    one of them: SQL put together any other way fails here.
    """
    skeleton, literals = _c_tokens(CATALOG_C.read_text())
    calls = re.findall(
        r"\b(sqlite3_prepare_v2|query_int)\s*\(\s*([^,()]+(?:\([^()]*\))?)"
        r"\s*,\s*(@\d+@|\w+)", skeleton)
    found: list[str] = []
    passed_on: list[str] = []
    for func, first, arg in calls:
        if "*" in first:
            continue        # the definition of query_int, not a call
        if arg.startswith("@"):
            found.append(literals[int(arg.strip("@"))])
        else:
            passed_on.append(f"{func}(..., {arg})")
    assert passed_on == ["sqlite3_prepare_v2(..., sql)"], (
        "a catalog.c query that is not a string literal at its call: "
        f"{passed_on}")
    sql_like = [s for s in literals if _SQL_START.match(s)]
    assert sorted(sql_like) == sorted(found), (
        "SQL in catalog.c outside a sqlite3_prepare_v2/query_int call: "
        f"{sorted(set(sql_like) - set(found))}")
    assert len(found) >= 10, f"catalog.c: only {len(found)} queries found"
    return found


def python_reader_sql() -> list[str]:
    """Every SQL statement the Python reader (tier 3) runs, from the
    ``python_reader.py`` that ``build_standalone()`` writes onto a META
    disc.  Each ``execute``-family call must take a string constant."""
    from lcsas.restore.standalone_builder import build_standalone

    tree = ast.parse(build_standalone())
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executemany",
                                       "executescript"}):
            continue
        arg = node.args[0] if node.args else None
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"python_reader.py line {node.lineno}: a query that is not a "
            "string constant")
        found.append(arg.value)
    assert found, "python_reader.py: no catalog query found"
    return found


def _bindings(sql: str) -> dict[str, None] | tuple[None, ...]:
    numbered = re.findall(r"\?(\d+)", sql)
    if numbered:
        return {num: None for num in numbered}
    return (None,) * sql.count("?")


def _reads(conn: sqlite3.Connection, sql: str) -> list[tuple[str, str]]:
    """What preparing *sql* on *conn* reads, by SQLite's authorizer:
    ``("table", "column")`` for each column read (``""`` when only the
    table is, as by ``COUNT(*)``), ``("PRAGMA", name)`` for a pragma,
    ``("ACTION", code)`` for anything else that is not a SELECT or a
    function call."""
    seen: list[tuple[str, str]] = []

    def auth(action: int, arg1: str | None, arg2: str | None,
             _db: str | None, _src: str | None) -> int:
        if action == sqlite3.SQLITE_READ:
            seen.append((str(arg1), str(arg2 or "")))
        elif action == sqlite3.SQLITE_PRAGMA:
            seen.append(("PRAGMA", str(arg1).lower()))
        elif action not in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            seen.append(("ACTION", str(action)))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(auth)
    try:
        conn.execute("EXPLAIN " + sql, _bindings(sql)).fetchall()
    finally:
        conn.set_authorizer(None)
    return seen


def _frozen_only_catalog() -> sqlite3.Connection:
    """A catalog holding the frozen surface and nothing else."""
    conn = sqlite3.connect(":memory:")
    for table, columns in TIER1_FROZEN_SURFACE.items():
        cols = ", ".join(columns) if columns else "frozen_table_only"
        conn.execute(f"CREATE TABLE {table} ({cols})")
    return conn


READERS = {"tier 1 (catalog.c)": c_reader_sql,
           "tier 3 (python_reader.py)": python_reader_sql}


@pytest.mark.parametrize("reader", READERS)
def test_every_reader_query_reads_only_the_frozen_surface(reader: str) -> None:
    """Each column a reader's query reads is on TIER1_FROZEN_SURFACE,
    each pragma in TIER1_FROZEN_PRAGMAS, and no query writes."""
    conn = _fresh_catalog()
    outside: list[str] = []
    try:
        for sql in READERS[reader]():
            for kind, name in _reads(conn, sql):
                if kind == "PRAGMA":
                    if name not in TIER1_FROZEN_PRAGMAS:
                        outside.append(f"PRAGMA {name}  <- {sql!r}")
                elif kind == "ACTION":
                    outside.append(f"authorizer action {name}  <- {sql!r}")
                elif kind not in TIER1_FROZEN_SURFACE or (
                        name and name not in TIER1_FROZEN_SURFACE[kind]):
                    outside.append(f"{kind}.{name or '*'}  <- {sql!r}")
    finally:
        conn.close()
    assert not outside, (
        f"{reader} reads outside TIER1_FROZEN_SURFACE (lcsas.db.schema); "
        "widen the surface on purpose, and FORMAT.txt with it:\n  "
        + "\n  ".join(sorted(set(outside))))


@pytest.mark.parametrize("reader", READERS)
def test_every_reader_query_prepares_on_the_frozen_surface_alone(
        reader: str) -> None:
    """A catalog holding the frozen surface and nothing more is enough
    for every query a reader runs."""
    conn = _frozen_only_catalog()
    failed: list[str] = []
    try:
        for sql in READERS[reader]():
            try:
                conn.execute("EXPLAIN " + sql, _bindings(sql)).fetchall()
            except sqlite3.Error as exc:
                failed.append(f"{exc}  <- {sql!r}")
    finally:
        conn.close()
    assert not failed, (
        f"{reader} needs more than TIER1_FROZEN_SURFACE:\n  "
        + "\n  ".join(failed))


# ── The surface in the schema ───────────────────────────────────────


def test_frozen_columns_present() -> None:
    conn = _fresh_catalog()
    try:
        for table, columns in TIER1_FROZEN_SURFACE.items():
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert rows, f"tier-1 frozen table {table!r} is missing"
            present = {row[1] for row in rows}
            for col in columns:
                assert col in present, (
                    f"tier-1 frozen column {table}.{col} is missing — "
                    f"this breaks catalog.c on every burned disc"
                )
    finally:
        conn.close()


def test_destroyed_status_still_valid() -> None:
    conn = _fresh_catalog()
    try:
        conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) "
            "VALUES ('r', 'r', '/srv')"
        )
        # The tier-1 'status != DESTROYED' filter requires DESTROYED to be an
        # accepted CHECK value; this INSERT must not raise IntegrityError.
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
            "status) VALUES ('v1', 'u1', 'BD25', 1, ?)",
            (TIER1_REQUIRED_VOLUME_STATUS,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM volumes WHERE label = 'v1'"
        ).fetchone()
        assert row[0] == TIER1_REQUIRED_VOLUME_STATUS
    finally:
        conn.close()


def test_both_readers_filter_on_the_required_status() -> None:
    """The one status value tier 1 filters on is the frozen one."""
    filters = {m for sql in c_reader_sql()
               for m in re.findall(r"status != '(\w+)'", sql)}
    assert filters == {TIER1_REQUIRED_VOLUME_STATUS}


def test_schema_version_is_read_as_its_highest_row() -> None:
    """#961: the highest ``version`` is the catalog's version, in tier 1
    as in Python.  A master migrated step by step keeps a row for each
    version it went through, oldest first."""
    assert "SELECT MAX(version) FROM schema_version" in c_reader_sql()


# ── The label mark (#676, #950) ─────────────────────────────────────


def test_tier1_reads_the_label_mark_and_nothing_else_from_the_header() -> None:
    pragmas = {sql.split()[1].lower() for sql in c_reader_sql()
               if sql.upper().startswith("PRAGMA")}
    assert TIER1_LABEL_MARK_PRAGMA in pragmas
    assert pragmas <= set(TIER1_FROZEN_PRAGMAS)
    assert "application_id" not in TIER1_FROZEN_PRAGMAS


def test_label_mark_holds_the_highest_label_number_issued(
        tmp_path: Path) -> None:
    """``PRAGMA user_version`` is the label mark and nothing else: the
    highest label number issued, never lowered.  ``application_id`` is
    left as SQLite makes it (0, unused)."""
    from lcsas.db.label_mark import label_high_water, raise_label_high_water

    conn = sqlite3.connect(tmp_path / "catalog.db")
    try:
        create_all(conn)
        assert conn.execute(
            f"PRAGMA {TIER1_LABEL_MARK_PRAGMA}").fetchone()[0] == 0
        raise_label_high_water(conn, 7)
        raise_label_high_water(conn, 3)
        conn.commit()
        assert label_high_water(conn) == 7
        assert conn.execute(
            f"PRAGMA {TIER1_LABEL_MARK_PRAGMA}").fetchone()[0] == 7
        assert conn.execute(
            "PRAGMA application_id").fetchone()[0] == CATALOG_APPLICATION_ID
    finally:
        conn.close()
    assert CATALOG_APPLICATION_ID == 0


# ── FORMAT.txt states the contract ──────────────────────────────────


def _format_catalog_section() -> str:
    text = FORMAT_TXT.read_text()
    start = text.index("\nLCSAS CATALOG\n")
    end = text.index("\nVOLUME LAYOUT", start)
    return " ".join(text[start:end].split())


def test_format_txt_lists_the_whole_frozen_surface() -> None:
    section = _format_catalog_section()
    for table, columns in TIER1_FROZEN_SURFACE.items():
        line = f"{table} ({', '.join(columns)})" if columns else (
            f"{table} (the table only)")
        assert line in section, f"FORMAT.txt does not list {line!r}"
    for pragma in TIER1_FROZEN_PRAGMAS:
        assert f"PRAGMA {pragma}" in section, pragma
    assert "PRAGMA application_id" in section
    assert "SQLite 3.46.0" in section
    assert "highest" in section and "schema_version" in section


def test_vendored_sqlite_is_the_version_format_txt_names() -> None:
    header = (ROOT / "recovery" / "vendored" / "sqlite" / "sqlite3.h").read_text()
    m = re.search(r'#define SQLITE_VERSION\s+"([\d.]+)"', header)
    assert m is not None
    assert f"SQLite {m.group(1)}" in _format_catalog_section()
