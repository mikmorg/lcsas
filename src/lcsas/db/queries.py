"""Complex cross-table queries for the LCSAS catalog."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Pack, Snapshot, Volume


def _row_to_pack(row: sqlite3.Row) -> Pack:
    return Pack(
        pack_id=row["pack_id"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        repo_id=row["repo_id"],
        is_pruned=bool(row["is_pruned"]),
        created_at=row["created_at"],
    )


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


def get_unarchived_packs(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
) -> list[Pack]:
    """Return packs not yet assigned to any volume (and not pruned).

    These are packs sitting on the Local Mirror that need to be burned.
    """
    if repo_id:
        rows = conn.execute(
            """SELECT p.* FROM packs p
               WHERE p.is_pruned = 0
                 AND p.repo_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id
                 )
               ORDER BY p.created_at""",
            (repo_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT p.* FROM packs p
               WHERE p.is_pruned = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id
                 )
               ORDER BY p.created_at"""
        ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_total_unarchived_bytes(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
) -> int:
    """Return total bytes of unarchived, non-pruned packs."""
    if repo_id:
        row = conn.execute(
            """SELECT COALESCE(SUM(p.size_bytes), 0) as total
               FROM packs p
               WHERE p.is_pruned = 0
                 AND p.repo_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id
                 )""",
            (repo_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COALESCE(SUM(p.size_bytes), 0) as total
               FROM packs p
               WHERE p.is_pruned = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id
                 )"""
        ).fetchone()
    assert row is not None
    return int(row[0])


def get_packs_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[Pack]:
    """Return all packs on a specific volume."""
    rows = conn.execute(
        """SELECT p.* FROM packs p
           JOIN volume_packs vp ON p.pack_id = vp.pack_id
           WHERE vp.volume_id = ?
           ORDER BY p.sha256""",
        (volume_id,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_volumes_for_pack(
    conn: sqlite3.Connection,
    pack_id: int,
) -> list[Volume]:
    """Return all volumes containing a specific pack (redundancy check)."""
    rows = conn.execute(
        """SELECT v.* FROM volumes v
           JOIN volume_packs vp ON v.volume_id = vp.volume_id
           WHERE vp.pack_id = ?
           ORDER BY v.label""",
        (pack_id,),
    ).fetchall()
    return [_row_to_volume(r) for r in rows]


def get_pick_list(
    conn: sqlite3.Connection,
    pack_sha256_list: list[str],
    preferred_location: str = "",
) -> dict[str, list[Pack]]:
    """Generate a restore 'pick list': map volume labels to needed packs.

    Given a list of required pack SHA-256 hashes (from a restore dry-run),
    returns a dict of {volume_label: [Pack, ...]} telling the user which
    discs to retrieve.

    Prefers non-DEPRECATED/DESTROYED volumes. If a pack exists on multiple
    volumes, prefers volumes at *preferred_location* (if specified) to
    minimise disc-swapping across locations. Falls back to alphabetical
    order.

    Args:
        conn: DB connection.
        pack_sha256_list: SHA-256 hashes of required packs.
        preferred_location: Optional storage location to prefer (e.g.
            ``"Home_Shelf"``).  Volumes at this location are chosen
            over volumes elsewhere when both carry the same pack.
    """
    if not pack_sha256_list:
        return {}

    placeholders = ",".join("?" for _ in pack_sha256_list)

    # Order by: preferred location first (desc so 1 sorts before 0),
    # then alphabetically by label for deterministic tie-breaking.
    if preferred_location:
        rows = conn.execute(
            f"""SELECT p.*, v.volume_id, v.label as vol_label,
                       v.status as vol_status, v.location as vol_location
                FROM packs p
                JOIN volume_packs vp ON p.pack_id = vp.pack_id
                JOIN volumes v ON vp.volume_id = v.volume_id
                WHERE p.sha256 IN ({placeholders})
                  AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                ORDER BY (CASE WHEN v.location = ? THEN 0 ELSE 1 END),
                         v.label""",
            [*pack_sha256_list, preferred_location],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT p.*, v.volume_id, v.label as vol_label,
                       v.status as vol_status
                FROM packs p
                JOIN volume_packs vp ON p.pack_id = vp.pack_id
                JOIN volumes v ON vp.volume_id = v.volume_id
                WHERE p.sha256 IN ({placeholders})
                  AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                ORDER BY v.label""",
            pack_sha256_list,
        ).fetchall()

    # Deduplicate: each pack assigned to one volume only
    seen_packs: set[str] = set()
    result: dict[str, list[Pack]] = {}

    for row in rows:
        pack = _row_to_pack(row)
        if pack.sha256 in seen_packs:
            continue
        seen_packs.add(pack.sha256)
        vol_label = row["vol_label"]
        result.setdefault(vol_label, []).append(pack)

    return result


def get_missing_packs(
    conn: sqlite3.Connection,
    pack_sha256_list: list[str],
) -> list[str]:
    """Return SHA-256 hashes from the input list that have no volume assignment."""
    if not pack_sha256_list:
        return []

    placeholders = ",".join("?" for _ in pack_sha256_list)
    rows = conn.execute(
        f"""SELECT p.sha256 FROM packs p
            WHERE p.sha256 IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id
              )""",
        pack_sha256_list,
    ).fetchall()
    archived = {r["sha256"] for r in conn.execute(
        f"SELECT sha256 FROM packs WHERE sha256 IN ({placeholders})",
        pack_sha256_list,
    ).fetchall()}

    # Packs not even in the DB
    missing = [h for h in pack_sha256_list if h not in archived]

    # Packs in DB but not on any volume
    for row in rows:
        if row["sha256"] not in missing:
            missing.append(row["sha256"])

    return missing


def get_packs_only_on_volumes(
    conn: sqlite3.Connection,
    volume_ids: list[int],
) -> list[Pack]:
    """Return active (non-pruned) packs that exist on the given volumes.

    Used during consolidation to identify which packs from source volumes
    should be migrated to a new target volume.
    """
    if not volume_ids:
        return []

    placeholders = ",".join("?" for _ in volume_ids)
    rows = conn.execute(
        f"""SELECT DISTINCT p.* FROM packs p
            JOIN volume_packs vp ON p.pack_id = vp.pack_id
            WHERE vp.volume_id IN ({placeholders})
              AND p.is_pruned = 0
            ORDER BY p.sha256""",
        volume_ids,
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_redundancy_report(
    conn: sqlite3.Connection,
    min_copies: int = 2,
) -> list[Pack]:
    """Return non-pruned packs with fewer than min_copies volume assignments.

    Useful for ensuring every pack is stored on at least N volumes.
    """
    rows = conn.execute(
        """SELECT p.*, COUNT(v.volume_id) as copy_count
           FROM packs p
           LEFT JOIN volume_packs vp ON p.pack_id = vp.pack_id
           LEFT JOIN volumes v ON vp.volume_id = v.volume_id
               AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
           WHERE p.is_pruned = 0
           GROUP BY p.pack_id
           HAVING copy_count < ?
           ORDER BY copy_count, p.sha256""",
        (min_copies,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_archive_status_summary(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Return a summary of archive status: total packs, archived, unarchived, pruned."""
    row = conn.execute(
        """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN is_pruned = 1 THEN 1 ELSE 0 END) as pruned,
               SUM(CASE WHEN is_pruned = 0 AND EXISTS (
                   SELECT 1 FROM volume_packs vp WHERE vp.pack_id = packs.pack_id
               ) THEN 1 ELSE 0 END) as archived,
               SUM(CASE WHEN is_pruned = 0 AND NOT EXISTS (
                   SELECT 1 FROM volume_packs vp WHERE vp.pack_id = packs.pack_id
               ) THEN 1 ELSE 0 END) as unarchived
           FROM packs"""
    ).fetchone()
    assert row is not None
    return {
        "total": int(row[0]),
        "pruned": int(row[1] or 0),
        "archived": int(row[2] or 0),
        "unarchived": int(row[3] or 0),
    }


def get_unarchived_or_missing_at_location(
    conn: sqlite3.Connection,
    location: str,
) -> list[Pack]:
    """Return packs that either have no volume at all OR have no ACTIVE copy
    at the specified location. This is the full set needed to bring a
    location completely up to date.
    """
    rows = conn.execute(
        """SELECT p.* FROM packs p
           WHERE p.is_pruned = 0
             AND p.pack_id NOT IN (
                 SELECT DISTINCT vp.pack_id
                 FROM volume_packs vp
                 JOIN volume_copies vc ON vc.volume_id = vp.volume_id
                 WHERE vc.location = ?
                   AND vc.status = 'ACTIVE'
             )
           ORDER BY p.created_at""",
        (location,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


# ---------------------------------------------------------------------------
# Location-aware queries
# ---------------------------------------------------------------------------


def get_packs_at_location(
    conn: sqlite3.Connection,
    location: str,
) -> set[int]:
    """Return set of pack IDs that have at least one ACTIVE copy at location."""
    rows = conn.execute(
        """SELECT DISTINCT vp.pack_id
           FROM volume_packs vp
           JOIN volume_copies vc ON vc.volume_id = vp.volume_id
           WHERE vc.location = ?
             AND vc.status = 'ACTIVE'""",
        (location,),
    ).fetchall()
    return {row["pack_id"] for row in rows}


def get_packs_missing_at_location(
    conn: sqlite3.Connection,
    location: str,
) -> list[Pack]:
    """Return packs that have been archived but have no ACTIVE copy at location.

    This identifies packs that need to be staged and burned for a location
    to bring it up to date.
    """
    rows = conn.execute(
        """SELECT p.* FROM packs p
           WHERE p.is_pruned = 0
             AND p.pack_id IN (SELECT pack_id FROM volume_packs)
             AND p.pack_id NOT IN (
                 SELECT DISTINCT vp.pack_id
                 FROM volume_packs vp
                 JOIN volume_copies vc ON vc.volume_id = vp.volume_id
                 WHERE vc.location = ?
                   AND vc.status = 'ACTIVE'
             )
           ORDER BY p.created_at""",
        (location,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_location_summary(
    conn: sqlite3.Connection,
) -> list[dict]:
    """Summary of each location: volume count, pack count, packs behind."""
    total_archived = conn.execute(
        """SELECT COUNT(DISTINCT pack_id) FROM volume_packs"""
    ).fetchone()[0]

    rows = conn.execute(
        """SELECT
               vc.location,
               COUNT(DISTINCT vc.volume_id) AS volume_count,
               COUNT(DISTINCT vp.pack_id) AS pack_count
           FROM volume_copies vc
           JOIN volume_packs vp ON vp.volume_id = vc.volume_id
           WHERE vc.status = 'ACTIVE'
           GROUP BY vc.location
           ORDER BY vc.location"""
    ).fetchall()
    return [
        {
            "location": r["location"],
            "volumes": r["volume_count"],
            "packs": r["pack_count"],
            "missing": total_archived - r["pack_count"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Snapshot JSON helpers  (requires SQLite 3.9+ for json_each)
# ---------------------------------------------------------------------------

def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        snapshot_id=row["snapshot_id"],
        repo_id=row["repo_id"],
        hostname=row["hostname"],
        timestamp=row["timestamp"],
        paths=row["paths"],
        tags=row["tags"],
        description=row["description"],
    )


def get_snapshots_by_path(
    conn: sqlite3.Connection,
    path_pattern: str,
    repo_id: str | None = None,
) -> list[Snapshot]:
    """Return snapshots containing a path matching *path_pattern*.

    Uses SQLite ``json_each()`` to search the JSON array stored in
    ``snapshots.paths``.  The *path_pattern* supports SQL LIKE wildcards
    (``%`` and ``_``).
    """
    if repo_id:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE s.repo_id = ?
                 AND EXISTS (
                     SELECT 1 FROM json_each(s.paths)
                     WHERE value LIKE ?
                 )
               ORDER BY s.timestamp DESC""",
            (repo_id, path_pattern),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE EXISTS (
                   SELECT 1 FROM json_each(s.paths)
                   WHERE value LIKE ?
               )
               ORDER BY s.timestamp DESC""",
            (path_pattern,),
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def get_snapshots_by_tag(
    conn: sqlite3.Connection,
    tag: str,
    repo_id: str | None = None,
) -> list[Snapshot]:
    """Return snapshots that contain the exact *tag*.

    Uses SQLite ``json_each()`` on the ``snapshots.tags`` JSON array.
    """
    if repo_id:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE s.repo_id = ?
                 AND EXISTS (
                     SELECT 1 FROM json_each(s.tags)
                     WHERE value = ?
                 )
               ORDER BY s.timestamp DESC""",
            (repo_id, tag),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE EXISTS (
                   SELECT 1 FROM json_each(s.tags)
                   WHERE value = ?
               )
               ORDER BY s.timestamp DESC""",
            (tag,),
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]
