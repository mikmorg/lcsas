"""SQLite connection management for LCSAS."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection to the archive catalog database.

    Enables WAL mode, foreign keys, and uses Row factory for
    dict-like access to query results.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def get_memory_connection() -> sqlite3.Connection:
    """Return an in-memory SQLite connection (for testing).

    Same pragmas as a file-backed connection.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn
