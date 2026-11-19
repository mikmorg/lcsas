"""CRUD operations for the volumes table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Volume
from lcsas.utils.timestamps import utc_now


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
    from lcsas.utils.timestamps import utc_now

    cursor = conn.execute(
        """INSERT INTO volumes (label, uuid, media_type, capacity_bytes, location,
                                status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (label, uuid, media_type, capacity_bytes, location, status, utc_now()),
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


def mark_closed(
    conn: sqlite3.Connection, volume_id: int, *, commit: bool = True,
) -> None:
    """Set the closed_at timestamp on a volume (finalization)."""
    now = utc_now()
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
            "SELECT * FROM volumes WHERE status = ? ORDER BY julianday(created_at), volume_id",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM volumes ORDER BY julianday(created_at), volume_id"
        ).fetchall()
    return [_row_to_volume(r) for r in rows]


def check_deprecation_safe(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[str]:
    """Return pack IDs that would become unreplicated if this volume is deprecated.

    A pack is "at risk" if it exists on this volume but on NO other volume
    that is a live replica.

    Replica truth (BURN-10): a volume is a live replica iff it has >=1
    ACTIVE row in ``volume_copies`` — except a volume with zero
    ``volume_copies`` rows at all, which counts by status alone (legacy:
    volumes burned before discs were recorded, skip_burn fixtures, catalogs
    rebuilt from old discs). A BURNED/VERIFIED volume whose every disc is
    DEPRECATED/DESTROYED is dead and must NOT count.

    Thin wrapper over ``get_at_risk_packs_for_volume`` (``db/queries.py``)
    so the deprecation guard and the ``lcsas volume impact`` blast-radius
    report (FMA-08) can never disagree.
    """
    from lcsas.db.queries import get_at_risk_packs_for_volume

    return [p.sha256 for p in get_at_risk_packs_for_volume(conn, volume_id)]
