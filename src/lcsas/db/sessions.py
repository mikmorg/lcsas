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
    return SessionVolume(
        session_id=row["session_id"],
        volume_id=row["volume_id"],
        iso_path=row["iso_path"],
        iso_sha256=row["iso_sha256"],
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
    # Verify it exists
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
    *,
    commit: bool = True,
) -> SessionVolume:
    """Link a volume to a session with its ISO path."""
    conn.execute(
        """INSERT INTO session_volumes (session_id, volume_id, iso_path, iso_sha256)
           VALUES (?, ?, ?, ?)""",
        (session_id, volume_id, iso_path, iso_sha256),
    )
    if commit:
        conn.commit()
    return SessionVolume(
        session_id=session_id,
        volume_id=volume_id,
        iso_path=iso_path,
        iso_sha256=iso_sha256,
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


def update_iso_sha256(
    conn: sqlite3.Connection,
    session_id: str,
    volume_id: int,
    iso_sha256: str,
) -> None:
    """Update the ISO SHA-256 hash for a session volume."""
    conn.execute(
        """UPDATE session_volumes SET iso_sha256 = ?
           WHERE session_id = ? AND volume_id = ?""",
        (iso_sha256, session_id, volume_id),
    )
    conn.commit()


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Delete a session and its session_volumes entries (atomic)."""
    with conn:
        conn.execute(
            "DELETE FROM session_volumes WHERE session_id = ?",
            (session_id,),
        )
        conn.execute(
            "DELETE FROM burn_sessions WHERE session_id = ?",
            (session_id,),
        )
