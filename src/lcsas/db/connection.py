"""SQLite connection management for LCSAS."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from lcsas.db.schema import CURRENT_SCHEMA_VERSION, get_schema_version
from lcsas.exceptions import CatalogLockTimeout, CatalogUnreadableError

# Command label written into the lock file so a waiter can name the holder.
# Set by the CLI before taking the lock; falls back to a generic label.
_HOLDER_CMD = "lcsas"


def set_lock_holder_label(label: str) -> None:
    """Record the command label stamped into the lock file when held.

    Called by the CLI dispatcher so a concurrent process that waits on the
    lock can print which command is holding it.
    """
    global _HOLDER_CMD
    _HOLDER_CMD = label


def _unreadable(db_path: Path | str, why: str) -> CatalogUnreadableError:
    """The refusal for a catalog file SQLite cannot read (#968)."""
    return CatalogUnreadableError(
        f"The catalog at {db_path} is damaged or is not a catalog ({why}). "
        "Nothing was changed.",
        recovery_hint=(
            "Restore the file from a backup copy, or rebuild the catalog "
            "from the discs: 'lcsas catalog rebuild --output <new catalog> "
            "<disc folder> ...', then point [paths] catalog at the new file."
        ),
    )


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection to the archive catalog.

    Enables WAL mode, foreign keys, busy_timeout, and uses Row factory
    for dict-like access to query results.  Sets the file to owner-only
    permissions (0600) on first creation.

    Catalog files are created with owner-only permissions atomically
    by using ``os.open()`` with ``O_CREAT | O_EXCL`` so the file is
    never world-readable even for an instant.
    """
    db_str = str(db_path)
    # The in-memory sentinel is NOT a filesystem path: Path(":memory:") plus
    # os.open(O_CREAT) below would create a junk file literally named
    # ":memory:" in the cwd (and locked_connection would add ":memory:.lock").
    # Skip the filesystem setup and connect in-memory directly.
    if db_str != ":memory:":
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        # Atomically create the file with restricted permissions so there is
        # no window where it is readable by other users (TOCTOU-safe).
        if not db.exists():
            fd = os.open(str(db), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
    conn = sqlite3.connect(db_str)
    # Everything after connect() can fail on a truncated or corrupt
    # catalog (the first statement is usually the first to read the
    # header), so close the connection before the error propagates (#769).
    try:
        conn.row_factory = sqlite3.Row
        # #968: the version first.  A file SQLite cannot read is refused
        # in words, and a catalog newer than this build keeps its journal
        # mode: ensure_schema refuses to write it, and a command that only
        # reads it must not change it either.
        try:
            version = get_schema_version(conn)
        except sqlite3.DatabaseError as exc:
            raise _unreadable(db_path, str(exc)) from exc
        if version <= CURRENT_SCHEMA_VERSION:
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA wal_autocheckpoint=1000;")  # explicit: checkpoint every 1000 pages
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        result = conn.execute("PRAGMA quick_check(1);").fetchone()
        if result is not None and result[0] != "ok":
            raise _unreadable(
                db_path, f"integrity check failed: {result[0]}")
    except Exception:
        conn.close()
        raise
    return conn


def _read_holder(lock_path: Path) -> str:
    """Describe the current lock holder from the lock file, best-effort.

    The holder JSON is written by whoever currently holds the lock; an
    empty/garbage file (older code, or a holder that died before stamping)
    yields a generic description rather than an error.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        info = json.loads(raw)
        cmd = info.get("cmd", "another lcsas command")
        pid = info.get("pid", "?")
        since = info.get("since", "")
        # Show only HH:MM of the ISO timestamp for a terse message.
        since_short = since[11:16] if len(since) >= 16 else since
        if since_short:
            return f"'{cmd}' (pid {pid}, since {since_short})"
        return f"'{cmd}' (pid {pid})"
    except (OSError, ValueError):
        return "another lcsas process"


def catalog_lock_holder(db_path: Path | str) -> dict[str, object] | None:
    """The stamp of the command holding the catalog lock, or None when the
    lock is free (#968: ``status`` tells a running stage from a dead one).

    A non-blocking probe of ``<db_path>.lock``: it never creates the lock
    file, never waits and never re-stamps it.  The stamp is what
    :func:`locked_connection` wrote (``pid``, ``cmd``, ``since``); an
    unreadable one is an empty dict.
    """
    lock_path = Path(str(db_path) + ".lock")
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                info = json.loads(lock_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return {}
            return info if isinstance(info, dict) else {}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextmanager
def locked_connection(
    db_path: Path | str,
    *,
    exclusive: bool = True,
    timeout: float | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that acquires a file lock around a catalog connection.

    Acquires an ``fcntl.flock(LOCK_EX)`` on ``<db_path>.lock`` before
    opening the SQLite connection and releases it on exit (including on
    exception).

    Parameters
    ----------
    db_path:
        Path to the catalog's SQLite file.
    exclusive:
        If *True* (default), use ``LOCK_EX``; otherwise ``LOCK_SH``.
    timeout:
        Maximum seconds to wait for the lock.  ``None`` (default) waits
        forever (the interactive default).  On expiry a
        :class:`CatalogLockTimeout` is raised naming the holder.
    """
    lock_path = Path(str(db_path) + ".lock")
    # open("a+") creates the file if absent and is inherently atomic —
    # no separate touch() needed, which avoided a TOCTOU window.  We need
    # read+write so we can both read a prior holder's stamp and rewrite ours.
    lock_fd = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
    try:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_fd, flag | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(lock_path)
            print(
                f"Waiting for the catalog lock: held by {holder}.\n"
                "Ctrl-C safely cancels THIS command. Do NOT kill the other "
                "process — it may be burning.",
                file=sys.stderr,
                flush=True,
            )
            if timeout is None:
                fcntl.flock(lock_fd, flag)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(lock_fd, flag | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise CatalogLockTimeout(
                                f"Timed out after {timeout:g}s waiting for the "
                                f"catalog lock: held by {_read_holder(lock_path)}."
                            ) from None
                        time.sleep(0.1)
        # Stamp our identity for the next waiter (older code never reads it).
        holder_json = json.dumps(
            {
                "pid": os.getpid(),
                "cmd": _HOLDER_CMD,
                "since": datetime.now().astimezone().isoformat(),
            }
        )
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(holder_json)
        lock_fd.flush()
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
