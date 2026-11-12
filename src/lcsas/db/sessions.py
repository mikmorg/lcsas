"""CRUD operations for the burn_sessions and session_volumes tables."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import BurnSession, SessionVolume
from lcsas.utils.labels import generate_session_id


def _row_to_session(row: sqlite3.Row) -> BurnSession:
    return BurnSession(
        session_id=row["session_id"],
        created_at=row["created_at"],
        media_type=row["media_type"],
        status=row["status"],
        staging_dir=row["staging_dir"],
    )


def _row_to_session_volume(row: sqlite3.Row) -> SessionVolume:
    # iso_size_bytes is absent on catalogs burned before schema v7
    # (disc catalogs are frozen forever).
    try:
        iso_size_bytes = row["iso_size_bytes"]
    except (IndexError, KeyError):
        iso_size_bytes = None
    return SessionVolume(
        session_id=row["session_id"],
        volume_id=row["volume_id"],
        iso_path=row["iso_path"],
        iso_sha256=row["iso_sha256"],
        iso_size_bytes=iso_size_bytes,
    )


def create_session(
    conn: sqlite3.Connection,
    media_type: str,
    staging_dir: str,
    session_id: str | None = None,
    *,
    commit: bool = True,
) -> BurnSession:
    """Create a new burn session."""
    if session_id is None:
        session_id = generate_session_id()
    conn.execute(
        """INSERT INTO burn_sessions (session_id, media_type, staging_dir)
           VALUES (?, ?, ?)""",
        (session_id, media_type, staging_dir),
    )
    if commit:
        conn.commit()
    return get_session(conn, session_id)


def get_session(conn: sqlite3.Connection, session_id: str) -> BurnSession:
    """Get a session by ID."""
    row = conn.execute(
        "SELECT * FROM burn_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Session '{session_id}' not found")
    return _row_to_session(row)


def get_latest_session(conn: sqlite3.Connection) -> BurnSession:
    """Get the most recently created session."""
    row = conn.execute(
        "SELECT * FROM burn_sessions ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("No sessions exist")
    return _row_to_session(row)


def resolve_session_id(conn: sqlite3.Connection, session_ref: str) -> str:
    """Resolve 'latest' or a session_id string to an actual session_id."""
    if session_ref == "latest":
        return get_latest_session(conn).session_id
    # Check it exists
    get_session(conn, session_ref)
    return session_ref


def update_session_status(
    conn: sqlite3.Connection,
    session_id: str,
    status: str,
) -> None:
    """Update session status (STAGED, PARTIAL, COMPLETE, CLEANED)."""
    conn.execute(
        "UPDATE burn_sessions SET status = ? WHERE session_id = ?",
        (status, session_id),
    )
    conn.commit()


def delete_empty_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Delete a burn session that holds no volume (#753).

    For a ``lcsas stage`` that failed before any volume of its session
    was finished.  Refuses a session that still has a volume.
    """
    held = conn.execute(
        "SELECT COUNT(*) FROM session_volumes WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    if held:
        raise ValueError(
            f"Session '{session_id}' still has {held} volume(s); not deleted"
        )
    conn.execute("DELETE FROM burn_sessions WHERE session_id = ?",
                 (session_id,))
    conn.commit()


def list_sessions(
    conn: sqlite3.Connection,
    status_filter: str | None = None,
) -> list[BurnSession]:
    """List all sessions, optionally filtered by status."""
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM burn_sessions WHERE status = ? ORDER BY created_at",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM burn_sessions ORDER BY created_at"
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def add_session_volume(
    conn: sqlite3.Connection,
    session_id: str,
    volume_id: int,
    iso_path: str,
    iso_sha256: str | None = None,
    iso_size_bytes: int | None = None,
    *,
    commit: bool = True,
) -> SessionVolume:
    """Link a volume to a session with its ISO path."""
    conn.execute(
        """INSERT INTO session_volumes
               (session_id, volume_id, iso_path, iso_sha256, iso_size_bytes)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, volume_id, iso_path, iso_sha256, iso_size_bytes),
    )
    if commit:
        conn.commit()
    return SessionVolume(
        session_id=session_id,
        volume_id=volume_id,
        iso_path=iso_path,
        iso_sha256=iso_sha256,
        iso_size_bytes=iso_size_bytes,
    )


def get_session_volumes(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[SessionVolume]:
    """Get all volumes in a session."""
    rows = conn.execute(
        "SELECT * FROM session_volumes WHERE session_id = ? ORDER BY volume_id",
        (session_id,),
    ).fetchall()
    return [_row_to_session_volume(r) for r in rows]


def update_session_volume_iso(
    conn: sqlite3.Connection,
    session_id: str,
    volume_id: int,
    iso_path: str,
    iso_sha256: str,
    iso_size_bytes: int | None = None,
) -> None:
    """Fill in the ISO path/hash/size for a session volume registered pre-ISO.

    Volumes are linked into session_volumes in the same transaction that
    creates them (before the ISO exists, with iso_sha256=''); this records
    the final hash and byte length once the ISO has been mastered.
    """
    conn.execute(
        """UPDATE session_volumes
           SET iso_path = ?, iso_sha256 = ?, iso_size_bytes = ?
           WHERE session_id = ? AND volume_id = ?""",
        (iso_path, iso_sha256, iso_size_bytes, session_id, volume_id),
    )
    conn.commit()


def get_staged_iso_path(
    conn: sqlite3.Connection,
    volume_id: int,
) -> str | None:
    """Return the ISO path recorded for the volume by its most recent burn
    session, or None if no session recorded one (#647)."""
    row = conn.execute(
        """SELECT sv.iso_path
           FROM session_volumes sv
           JOIN burn_sessions bs USING (session_id)
           WHERE sv.volume_id = ? AND sv.iso_path != ''
           ORDER BY bs.created_at DESC, sv.rowid DESC
           LIMIT 1""",
        (volume_id,),
    ).fetchone()
    return str(row["iso_path"]) if row is not None else None


def staged_iso_present(conn: sqlite3.Connection, volume_id: int) -> bool:
    """True when the volume's staged ISO still exists on disk: it is waiting
    to be burned (here, or elsewhere via burn-iso), not a ghost (#647)."""
    from pathlib import Path

    path = get_staged_iso_path(conn, volume_id)
    return path is not None and Path(path).is_file()


def get_staged_iso(
    conn: sqlite3.Connection,
    volume_id: int,
) -> tuple[str, int | None] | None:
    """Return ``(iso_sha256, iso_size_bytes)`` recorded when the volume's ISO
    was mastered, from its most recent burn session, or None if no session
    recorded a hash for it (#644)."""
    row = conn.execute(
        """SELECT sv.iso_sha256, sv.iso_size_bytes
           FROM session_volumes sv
           JOIN burn_sessions bs USING (session_id)
           WHERE sv.volume_id = ?
             AND sv.iso_sha256 IS NOT NULL AND sv.iso_sha256 != ''
           ORDER BY bs.created_at DESC, sv.rowid DESC
           LIMIT 1""",
        (volume_id,),
    ).fetchone()
    if row is None:
        return None
    size = row["iso_size_bytes"]
    return str(row["iso_sha256"]), (int(size) if size is not None else None)


def staging_incomplete(conn: sqlite3.Connection, volume_id: int) -> bool:
    """True when the volume was staged but its ISO was never finished (#665).

    ``lcsas stage`` records the ISO's hash on the session volume just
    before the ISO gets its final name, after ECC and the size check.  A
    never-burned (STAGING/BURNING) volume whose most recent session row
    still has an empty hash was interrupted part-way: its ISO, if any,
    may lack RS03 data and must not be burned or imported.  The way out
    is ``lcsas session abort`` and a new ``lcsas stage``.

    Burned volumes are never reported: rows from before ISO hashes were
    recorded also have an empty hash.
    """
    row = conn.execute(
        """SELECT v.status, sv.iso_sha256
           FROM volumes v
           JOIN session_volumes sv ON sv.volume_id = v.volume_id
           JOIN burn_sessions bs ON bs.session_id = sv.session_id
           WHERE v.volume_id = ?
           ORDER BY bs.created_at DESC, sv.rowid DESC
           LIMIT 1""",
        (volume_id,),
    ).fetchone()
    if row is None:
        return False
    return row["status"] in ("STAGING", "BURNING") and not row["iso_sha256"]
