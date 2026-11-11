"""CRUD operations for the volume_packs junction table."""

from __future__ import annotations

import sqlite3


def bulk_link_packs(
    conn: sqlite3.Connection,
    volume_id: int,
    pack_ids: list[int],
    *,
    commit: bool = True,
) -> None:
    """Link multiple packs to a volume in a single transaction."""
    conn.executemany(
        "INSERT OR IGNORE INTO volume_packs (volume_id, pack_id) VALUES (?, ?)",
        [(volume_id, pid) for pid in pack_ids],
    )
    if commit:
        conn.commit()


def get_pack_ids_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[int]:
    """Return the ``pack_id`` row ids of all packs on a given volume."""
    rows = conn.execute(
        "SELECT pack_id FROM volume_packs WHERE volume_id = ?",
        (volume_id,),
    ).fetchall()
    return [r["pack_id"] for r in rows]


def get_volume_ids_for_pack(
    conn: sqlite3.Connection,
    pack_id: int,
) -> list[int]:
    """Return all volume IDs that contain a given pack."""
    rows = conn.execute(
        "SELECT volume_id FROM volume_packs WHERE pack_id = ?",
        (pack_id,),
    ).fetchall()
    return [r["volume_id"] for r in rows]
