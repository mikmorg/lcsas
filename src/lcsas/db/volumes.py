"""CRUD operations for the volumes table."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from lcsas.db.models import Volume

logger = logging.getLogger(__name__)

# Valid status transitions. Each key maps to the set of statuses
# that a volume is allowed to transition *to* from that state.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "STAGING":    {"BURNING", "DEPRECATED", "DESTROYED"},
    "BURNING":    {"BURNED", "VERIFIED", "STAGING", "DESTROYED"},  # VERIFIED = immediate verify
    "BURNED":     {"VERIFIED", "STAGING", "DESTROYED"},            # STAGING = re-burn
    "VERIFIED":   {"DEPRECATED", "DESTROYED"},
    "DEPRECATED": {"DESTROYED"},
    "DESTROYED":  set(),
}


def _row_to_volume(row: sqlite3.Row) -> Volume:
    # verified_at may be absent on catalogs from schema v2
    try:
        verified_at = row["verified_at"]
    except (IndexError, KeyError):
        verified_at = None
    return Volume(
        volume_id=row["volume_id"],
        label=row["label"],
        uuid=row["uuid"],
        media_type=row["media_type"],
        capacity_bytes=row["capacity_bytes"],
        used_bytes=row["used_bytes"],
        location=row["location"],
        status=row["status"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        verified_at=verified_at,
    )


def create_volume(
    conn: sqlite3.Connection,
    label: str,
    uuid: str,
    media_type: str,
    capacity_bytes: int,
    location: str = "Home_Shelf",
    status: str = "STAGING",
    *,
    commit: bool = True,
) -> Volume:
    """Insert a new volume and return the created Volume object."""
    cursor = conn.execute(
        """INSERT INTO volumes (label, uuid, media_type, capacity_bytes, location, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (label, uuid, media_type, capacity_bytes, location, status),
    )
    if commit:
        conn.commit()
    return get_volume_by_id(conn, cursor.lastrowid)  # type: ignore[arg-type]


def get_volume_by_id(conn: sqlite3.Connection, volume_id: int) -> Volume:
    """Fetch a volume by its primary key. Raises ValueError if not found."""
    row = conn.execute(
        "SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Volume with id {volume_id} not found")
    return _row_to_volume(row)


def get_volume_by_label(conn: sqlite3.Connection, label: str) -> Volume | None:
    """Fetch a volume by its human-readable label."""
    row = conn.execute(
        "SELECT * FROM volumes WHERE label = ?", (label,)
    ).fetchone()
    return _row_to_volume(row) if row else None


def get_volume_by_uuid(conn: sqlite3.Connection, uuid: str) -> Volume | None:
    """Fetch a volume by its machine UUID."""
    row = conn.execute(
        "SELECT * FROM volumes WHERE uuid = ?", (uuid,)
    ).fetchone()
    return _row_to_volume(row) if row else None


def update_status(
    conn: sqlite3.Connection,
    volume_id: int,
    status: str,
    *,
    commit: bool = True,
    force: bool = False,
) -> None:
    """Update the status of a volume.

    Enforces valid state transitions unless *force* is ``True``.
    Automatically sets ``verified_at`` when status transitions to VERIFIED.
    """
    if not force:
        current = get_volume_by_id(conn, volume_id).status
        allowed = VALID_TRANSITIONS.get(current, set())
        if status not in allowed:
            raise ValueError(
                f"Invalid status transition for volume {volume_id}: "
                f"{current} → {status} (allowed: {sorted(allowed)})"
            )
    elif force:
        logger.warning(
            "Forced status change for volume %d → %s (bypassing transition rules)",
            volume_id, status,
        )

    if status == "VERIFIED":
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE volumes SET status = ?, verified_at = ? WHERE volume_id = ?",
            (status, now, volume_id),
        )
    else:
        conn.execute(
            "UPDATE volumes SET status = ? WHERE volume_id = ?",
            (status, volume_id),
        )
    if commit:
        conn.commit()


def mark_closed(
    conn: sqlite3.Connection, volume_id: int, *, commit: bool = True,
) -> None:
    """Set the closed_at timestamp on a volume (finalization)."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE volumes SET closed_at = ? WHERE volume_id = ?",
        (now, volume_id),
    )
    if commit:
        conn.commit()


def update_used_bytes(
    conn: sqlite3.Connection,
    volume_id: int,
    used_bytes: int,
    *,
    commit: bool = True,
) -> None:
    """Update the used_bytes counter on a volume."""
    conn.execute(
        "UPDATE volumes SET used_bytes = ? WHERE volume_id = ?",
        (used_bytes, volume_id),
    )
    if commit:
        conn.commit()


def list_volumes(
    conn: sqlite3.Connection,
    status_filter: str | None = None,
) -> list[Volume]:
    """List all volumes, optionally filtered by status."""
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM volumes WHERE status = ? ORDER BY created_at",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM volumes ORDER BY created_at"
        ).fetchall()
    return [_row_to_volume(r) for r in rows]


def delete_volume(conn: sqlite3.Connection, volume_id: int) -> None:
    """Delete a volume (and cascade via FK if enabled). Use with caution."""
    conn.execute("DELETE FROM volume_packs WHERE volume_id = ?", (volume_id,))
    conn.execute("DELETE FROM volumes WHERE volume_id = ?", (volume_id,))
    conn.commit()
