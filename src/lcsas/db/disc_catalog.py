"""Open the catalog on a disc, read-only (#948).

A disc catalog (``catalog.db`` at the root of every data disc) lives on a
read-only medium: a mounted ISO 9660 disc, or an image extracted with
``xorriso -osirrox`` whose files are mode 0444.  ``get_connection`` is for
the owner's own catalog: it sets ``journal_mode=WAL`` first, which writes,
and fails there with "attempt to write a readonly database".

:func:`open_disc_catalog` opens such a catalog with ``mode=ro&immutable=1``:
SQLite writes nothing, takes no lock and makes no ``-wal``/``-shm`` file
beside it (a file left open there also keeps a disc from being ejected).
A catalog of a newer schema than this LCSAS knows is refused with words,
not read half-understood.
"""
from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path

from lcsas.db.schema import CURRENT_SCHEMA_VERSION
from lcsas.exceptions import CatalogError


class DiscCatalogError(CatalogError):
    """A disc catalog cannot be opened or is not one this LCSAS reads."""


def _schema_version(conn: sqlite3.Connection) -> int | None:
    """The catalog's schema version; None when it has no version table."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return int(row[0]) if row and row[0] is not None else None


def open_disc_catalog(path: Path) -> sqlite3.Connection:
    """A read-only connection to the disc catalog at *path*.

    Rows come back as ``sqlite3.Row``.  The caller closes the
    connection.

    Raises:
        DiscCatalogError: the file cannot be opened, is not an LCSAS
            catalog, or has a newer schema than this LCSAS reads.
    """
    if not path.is_file():
        raise DiscCatalogError(f"No catalog file at {path}.")
    # Percent-encoded, as in rebuild.py: mount paths may hold '?', '#'
    # or spaces.
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DiscCatalogError(
            f"Cannot open the disc catalog {path}: {exc}."
        ) from exc
    try:
        conn.row_factory = sqlite3.Row
        try:
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
        if version > CURRENT_SCHEMA_VERSION:
            raise DiscCatalogError(
                f"The disc catalog {path} is schema v{version}; this LCSAS "
                f"reads up to v{CURRENT_SCHEMA_VERSION}.  It was written by a "
                "newer LCSAS: use that version (the meta disc of this "
                "archive carries it), or restore with the restore.sh of "
                "the meta disc."
            )
    except BaseException:
        conn.close()
        raise
    return conn
