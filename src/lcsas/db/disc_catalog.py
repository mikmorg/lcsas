"""Open the catalog on a disc, read-only (#948, #963).

A disc catalog (``catalog.db`` at the root of every data disc) lives on a
read-only medium: a mounted ISO 9660 disc, or an image extracted with
``xorriso -osirrox`` whose files are mode 0444.  ``get_connection`` is for
the owner's own catalog: it sets ``journal_mode=WAL`` first, which writes,
and fails there with "attempt to write a readonly database".

:func:`open_disc_catalog` opens such a catalog with ``mode=ro&immutable=1``:
SQLite writes nothing, takes no lock and makes no ``-wal``/``-shm`` file
beside it (a file left open there also keeps a disc from being ejected).
A ``-wal`` or hot ``-journal`` file that a careless copy left beside the
catalog is neither read nor replayed: the catalog is read as it was
mastered.  ``PRAGMA quick_check`` must pass, so a truncated or damaged
file is refused with SQLite's reason instead of half-read.

Every reader of a disc catalog opens it here: ``restore from-disc``,
``catalog validate`` and ``catalog rebuild``.  :func:`inspect_disc_catalog`
gives the worded verdict (fine, older, newer, unreadable) without keeping
the file open.
"""
from __future__ import annotations

import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lcsas.db.schema import CURRENT_SCHEMA_VERSION
from lcsas.exceptions import CatalogError


class DiscCatalogError(CatalogError):
    """A disc catalog cannot be opened or is not one this LCSAS reads."""


def disc_catalog_uri(path: Path) -> str:
    """The read-only, immutable SQLite URI of the disc catalog at *path*.

    Percent-encoded: mount paths may hold '?', '#' or spaces.
    """
    return "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"


def _schema_version(conn: sqlite3.Connection) -> int | None:
    """The catalog's schema version (its highest row); None when it has no
    version table or no row."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return int(row[0]) if row and row[0] is not None else None


@dataclass(frozen=True)
class DiscCatalogState:
    """What a disc catalog is, in words (#963).

    ``kind`` is ``fine`` (this LCSAS's schema), ``older``, ``newer`` or
    ``unreadable``; ``version`` is its schema version when one was read;
    ``reason`` is SQLite's reason (or ours) for ``unreadable``.
    """

    path: Path
    kind: Literal["fine", "older", "newer", "unreadable"]
    version: int | None = None
    reason: str = ""

    def describe(self) -> str:
        """One sentence for the owner."""
        if self.kind == "unreadable":
            return f"cannot be read ({self.reason})"
        if self.kind == "newer":
            return (f"was written by a newer LCSAS (schema v{self.version}; "
                    f"this LCSAS knows up to v{CURRENT_SCHEMA_VERSION})")
        if self.kind == "older":
            return (f"was written by an older LCSAS (schema v{self.version}; "
                    f"this LCSAS writes v{CURRENT_SCHEMA_VERSION})")
        return f"is schema v{self.version}, as this LCSAS writes"


def _open_checked(path: Path) -> tuple[sqlite3.Connection, int]:
    """Open *path* read-only, run ``quick_check``, read its version.

    Raises DiscCatalogError (worded) for a missing, unreadable or
    non-LCSAS file.  Does not judge the version.
    """
    if not path.is_file():
        raise DiscCatalogError(f"No catalog file at {path}.")
    try:
        conn = sqlite3.connect(disc_catalog_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise DiscCatalogError(
            f"Cannot open the disc catalog {path}: {exc}."
        ) from exc
    try:
        conn.row_factory = sqlite3.Row
        try:
            check = [str(r[0]) for r in
                     conn.execute("PRAGMA quick_check(3)").fetchall()]
            if check != ["ok"]:
                raise sqlite3.DatabaseError("; ".join(check) or "no answer")
            version = _schema_version(conn)
        except sqlite3.DatabaseError as exc:
            raise DiscCatalogError(
                f"Cannot read the disc catalog {path}: {exc}.  The disc may "
                "be damaged: try another disc of the same archive."
            ) from exc
        if version is None:
            raise DiscCatalogError(
                f"{path} is not an LCSAS catalog (it has no schema "
                "version).  Try another disc of the same archive."
            )
    except BaseException:
        conn.close()
        raise
    return conn, version


def inspect_disc_catalog(path: Path) -> DiscCatalogState:
    """The worded state of the disc catalog at *path*; never raises for a
    bad file (that is the ``unreadable`` state)."""
    try:
        conn, version = _open_checked(path)
    except DiscCatalogError as exc:
        return DiscCatalogState(path, "unreadable", reason=str(exc))
    conn.close()
    if version > CURRENT_SCHEMA_VERSION:
        return DiscCatalogState(path, "newer", version)
    if version < CURRENT_SCHEMA_VERSION:
        return DiscCatalogState(path, "older", version)
    return DiscCatalogState(path, "fine", version)


def open_disc_catalog(
    path: Path, *, allow_newer: bool = False,
) -> sqlite3.Connection:
    """A read-only connection to the disc catalog at *path*.

    Rows come back as ``sqlite3.Row``.  The caller closes the
    connection.  *allow_newer* lets ``catalog rebuild`` read a catalog of
    a newer schema, which checks every value it merges; every other
    reader refuses one.

    Raises:
        DiscCatalogError: the file cannot be opened, fails its quick
            check, is not an LCSAS catalog, or (without *allow_newer*)
            has a newer schema than this LCSAS reads.
    """
    conn, version = _open_checked(path)
    if version > CURRENT_SCHEMA_VERSION and not allow_newer:
        conn.close()
        raise DiscCatalogError(
            f"The disc catalog {path} is schema v{version}; this LCSAS "
            f"reads up to v{CURRENT_SCHEMA_VERSION}.  It was written by a "
            "newer LCSAS: use that version (the meta disc of this "
            "archive carries it), or restore with the restore.sh of "
            "the meta disc."
        )
    return conn
