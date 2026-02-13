"""CRUD operations for the volume_packs junction table."""

from __future__ import annotations

import sqlite3


def link_pack_to_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    pack_id: int,
) -> None:
    """Create an association between a pack and a volume."""
    conn.execute(
        "INSERT OR IGNORE INTO volume_packs (volume_id, pack_id) VALUES (?, ?)",
        (volume_id, pack_id),
    )
    conn.commit()


def bulk_link_packs(
    conn: sqlite3.Connection,
    volume_id: int,
    pack_ids: list[int],
) -> None:
    """Link multiple packs to a volume in a single transaction."""
    conn.executemany(
        "INSERT OR IGNORE INTO volume_packs (volume_id, pack_id) VALUES (?, ?)",
        [(volume_id, pid) for pid in pack_ids],
    )
    conn.commit()


def unlink_pack_from_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    pack_id: int,
) -> None:
    """Remove the association between a pack and a volume."""
    conn.execute(
        "DELETE FROM volume_packs WHERE volume_id = ? AND pack_id = ?",
        (volume_id, pack_id),
    )
    conn.commit()


def get_pack_ids_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[int]:
    """Return all pack IDs associated with a given volume."""
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
