"""CRUD operations for the volume_copies table."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from lcsas.db.models import VolumeCopy


def _row_to_copy(row: sqlite3.Row) -> VolumeCopy:
    # iso_sha256, last_verified_at, media_serial may be absent on v3 catalogs
    try:
        iso_sha256 = row["iso_sha256"]
    except (IndexError, KeyError):
        iso_sha256 = None
    try:
        last_verified_at = row["last_verified_at"]
    except (IndexError, KeyError):
        last_verified_at = None
    try:
        media_serial = row["media_serial"]
    except (IndexError, KeyError):
        media_serial = ""
    return VolumeCopy(
        id=row["id"],
        volume_id=row["volume_id"],
        location=row["location"],
        status=row["status"],
        burn_date=row["burn_date"],
        notes=row["notes"],
        iso_sha256=iso_sha256,
        last_verified_at=last_verified_at,
        media_serial=media_serial,
    )


def add_volume_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    burn_date: str | None = None,
    notes: str = "",
    *,
    iso_sha256: str | None = None,
    media_serial: str = "",
    commit: bool = True,
) -> VolumeCopy:
    """Record a physical copy of a volume at a location."""
    if burn_date is None:
        burn_date = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """INSERT INTO volume_copies
               (volume_id, location, burn_date, notes, iso_sha256, media_serial)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (volume_id, location, burn_date, notes, iso_sha256, media_serial),
    )
    if commit:
        conn.commit()
    return get_volume_copy(conn, cursor.lastrowid)


def get_volume_copy(conn: sqlite3.Connection, copy_id: int) -> VolumeCopy:
    """Get a volume copy by its ID."""
    row = conn.execute(
        "SELECT * FROM volume_copies WHERE id = ?", (copy_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Volume copy {copy_id} not found")
    return _row_to_copy(row)


def get_copies_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    active_only: bool = True,
) -> list[VolumeCopy]:
    """Get all copies of a specific volume."""
    if active_only:
        rows = conn.execute(
            "SELECT * FROM volume_copies WHERE volume_id = ? AND status = 'ACTIVE'",
            (volume_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM volume_copies WHERE volume_id = ?",
            (volume_id,),
        ).fetchall()
    return [_row_to_copy(r) for r in rows]


def get_copies_at_location(
    conn: sqlite3.Connection,
    location: str,
    active_only: bool = True,
) -> list[VolumeCopy]:
    """Get all volume copies at a specific location."""
    if active_only:
        rows = conn.execute(
            "SELECT * FROM volume_copies WHERE location = ? AND status = 'ACTIVE'",
            (location,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM volume_copies WHERE location = ?",
            (location,),
        ).fetchall()
    return [_row_to_copy(r) for r in rows]


def move_volume_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    from_location: str,
    to_location: str,
) -> None:
    """Record a physical disc moving from one location to another."""
    now = datetime.now(UTC).isoformat()
    result = conn.execute(
        """UPDATE volume_copies
           SET location = ?,
               notes = COALESCE(notes, '') || 'Moved from ' || ? || ' on ' || ? || char(10)
           WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'""",
        (to_location, from_location, now, volume_id, from_location),
    )
    if result.rowcount == 0:
        raise ValueError(
            f"No active copy of volume {volume_id} at '{from_location}'"
        )
    conn.commit()


def deprecate_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
) -> None:
    """Mark a volume copy as deprecated (e.g. disc damaged)."""
    conn.execute(
        """UPDATE volume_copies SET status = 'DEPRECATED'
           WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'""",
        (volume_id, location),
    )
    conn.commit()


def destroy_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
) -> None:
    """Mark a volume copy as destroyed."""
    conn.execute(
        """UPDATE volume_copies SET status = 'DESTROYED'
           WHERE volume_id = ? AND location = ?""",
        (volume_id, location),
    )
    conn.commit()
