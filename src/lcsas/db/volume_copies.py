"""CRUD operations for the volume_copies table."""

from __future__ import annotations

import json
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
    """Record a physical copy of a volume at a location.

    If a copy already exists at this location (re-burn), the burn_date
    and notes are updated in-place.
    """
    if burn_date is None:
        burn_date = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO volume_copies
               (volume_id, location, burn_date, notes, iso_sha256, media_serial)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(volume_id, location) DO UPDATE SET
               burn_date    = excluded.burn_date,
               notes        = excluded.notes,
               status       = 'ACTIVE',
               iso_sha256   = excluded.iso_sha256,
               media_serial = excluded.media_serial""",
        (volume_id, location, burn_date, notes, iso_sha256, media_serial),
    )
    if commit:
        conn.commit()
    # Fetch by (volume_id, location) instead of lastrowid (unreliable after UPSERT on SQLite < 3.35)
    row = conn.execute(
        "SELECT * FROM volume_copies WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'",
        (volume_id, location),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"Failed to insert/update volume copy for volume {volume_id} at {location}"
        )
    return _row_to_copy(row)


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


def get_iso_sha256_for_label(
    conn: sqlite3.Connection,
    volume_label: str,
) -> str | None:
    """Return the recorded ISO SHA-256 for a volume label, or None.

    Phase 21.3 helper for the portable verifier.  Looks first at
    ``volume_copies`` (one row per location, written at burn time when
    a copy lands at a location), then falls back to the most recent
    ``session_volumes`` row for the same volume (covers the staged-but-
    not-yet-burned-to-a-location case).

    Returns the first non-null hash found.  Returns ``None`` if the
    volume label is unknown OR none of its records carry a hash
    (older v3 catalogs, or copies recorded before Phase 13).
    """
    from lcsas.db.volumes import get_volume_by_label

    vol = get_volume_by_label(conn, volume_label)
    if vol is None:
        return None

    # Prefer volume_copies — per-location, written at burn-to-disc time.
    for copy in get_copies_for_volume(conn, vol.volume_id, active_only=False):
        if copy.iso_sha256:
            return copy.iso_sha256

    # Fallback: session_volumes — written at ISO mastering time.  Pick
    # the latest session for this volume to favor the most recent burn.
    row = conn.execute(
        """SELECT sv.iso_sha256
           FROM session_volumes sv
           JOIN burn_sessions bs USING (session_id)
           WHERE sv.volume_id = ?
             AND sv.iso_sha256 IS NOT NULL
           ORDER BY bs.created_at DESC
           LIMIT 1""",
        (vol.volume_id,),
    ).fetchone()
    if row and row["iso_sha256"]:
        return str(row["iso_sha256"])
    return None


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
    """Record a physical disc moving from one location to another.

    Also emits a LOCATION_MOVE row in ``volume_events`` so the audit trail
    captures every physical move (issue #16). The update and the event
    insert share a single transaction so they commit atomically.
    """
    now = datetime.now(UTC).isoformat()
    try:
        result = conn.execute(
            """UPDATE volume_copies
               SET location = ?,
                   notes = COALESCE(notes, '') || 'Moved from ' || ? || ' on ' || ? || char(10)
               WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'""",
            (to_location, from_location, now, volume_id, from_location),
        )
    except sqlite3.IntegrityError:
        raise ValueError(
            f"A copy of volume {volume_id} already exists at '{to_location}'. "
            f"The disc cannot be moved there without first removing the existing copy."
        ) from None
    if result.rowcount == 0:
        raise ValueError(
            f"No active copy of volume {volume_id} at '{from_location}'"
        )
    # Audit trail: record the move as a LOCATION_MOVE event. The new location
    # goes in the dedicated `location` column; from/to are also serialised
    # into `detail` so the original location is recoverable from the event row
    # alone. Same connection -> same transaction as the UPDATE above.
    detail = json.dumps(
        {"from_location": from_location, "to_location": to_location}
    )
    conn.execute(
        """INSERT INTO volume_events (volume_id, event_type, event_date, location, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (volume_id, "LOCATION_MOVE", now, to_location, detail),
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
