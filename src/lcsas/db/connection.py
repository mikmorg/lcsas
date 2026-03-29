"""SQLite connection management for LCSAS."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection to the archive catalog database.

    Enables WAL mode, foreign keys, busy_timeout, and uses Row factory
    for dict-like access to query results.  Sets the file to owner-only
    permissions (0600) on first creation.

    Database files are created with owner-only permissions atomically
    by using ``os.open()`` with ``O_CREAT | O_EXCL`` so the file is
    never world-readable even for an instant.
    """
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    # Atomically create the file with restricted permissions so there is
    # no window where it is readable by other users (TOCTOU-safe).
    if not db.exists():
        fd = os.open(str(db), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")  # explicit: checkpoint every 1000 pages
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    result = conn.execute("PRAGMA quick_check(1);").fetchone()
    if result is not None and result[0] != "ok":
        raise RuntimeError(
            f"Database integrity check failed for '{db_path}': {result[0]}. "
            "The database may be corrupted. Restore from backup before continuing."
        )
    return conn


@contextmanager
def locked_connection(
    db_path: Path | str,
    *,
    exclusive: bool = True,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that acquires a file lock around a DB connection.

    Acquires an ``fcntl.flock(LOCK_EX)`` on ``<db_path>.lock`` before
    opening the SQLite connection and releases it on exit (including on
    exception).

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    exclusive:
        If *True* (default), use ``LOCK_EX``; otherwise ``LOCK_SH``.
    """
    lock_path = Path(str(db_path) + ".lock")
    # open("a") creates the file if absent and is inherently atomic —
    # no separate touch() needed, which avoided a TOCTOU window.
    lock_fd = open(lock_path, "a", encoding="utf-8")  # noqa: SIM115
    try:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_fd, flag)
        conn = get_connection(db_path)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def get_memory_connection() -> sqlite3.Connection:
    """Return an in-memory SQLite connection (for testing).

    Same pragmas as a file-backed connection.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn
