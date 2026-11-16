"""CRUD operations for the snapshots table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Snapshot
from lcsas.utils.timestamps import utc_now

# #733: the snapshot columns that a disc catalog does not carry, each
# with the blank value it gets there (the column's own "none" value).
# Every restore tier reads a snapshot's source folders, tags and
# description from rustic's own snapshot file, which is encrypted under
# the repository password; the catalog's copy is a plaintext duplicate.
# ``HolographicInjector.inject_catalog`` blanks these columns in each disc
# catalog; the owner's master catalog keeps them.  ``catalog rebuild``
# fills a blanked row from an older disc that still has them, and the
# next ``lcsas scan`` writes paths and tags again from rustic.
DISC_BLANKED_SNAPSHOT_COLUMNS: dict[str, str] = {
    "paths": "[]",
    "tags": "[]",
    "description": "",
}


# #824: the note ``lcsas scan`` writes in ``description`` for a snapshot
# that is no longer in the mirror (``rustic forget``) but that a disc may
# still hold.  Scans never store rustic's own description (they write
# ""), so the column is free for it; a snapshot that comes back is
# upserted with "" again.  ``list_snapshots`` leaves these rows out
# unless asked.
FORGOTTEN_NOTE_PREFIX = "LCSAS: not in the mirror"


def _forgotten_note(today: str) -> str:
    return (
        f"{FORGOTTEN_NOTE_PREFIX} since {today} (rustic forget). "
        "A disc burned before then may still hold it."
    )


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    """Convert a sqlite3.Row to a Snapshot model."""
    return Snapshot(
        snapshot_id=row["snapshot_id"],
        repo_id=row["repo_id"],
        hostname=row["hostname"],
        timestamp=row["timestamp"],
        paths=row["paths"],
        tags=row["tags"],
        description=row["description"],
    )


def upsert_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
    repo_id: str,
    hostname: str = "",
    timestamp: str = "",
    paths: str = "[]",
    tags: str = "[]",
    description: str = "",
    *,
    commit: bool = True,
) -> Snapshot:
    """Insert or replace a snapshot row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO snapshots
            (snapshot_id, repo_id, hostname, timestamp, paths, tags, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (snapshot_id, repo_id, hostname, timestamp, paths, tags, description),
    )
    if commit:
        conn.commit()
    return Snapshot(
        snapshot_id=snapshot_id,
        repo_id=repo_id,
        hostname=hostname,
        timestamp=timestamp,
        paths=paths,
        tags=tags,
        description=description,
    )


def bulk_upsert_snapshots(
    conn: sqlite3.Connection,
    snapshots: list[Snapshot],
    *,
    commit: bool = True,
) -> int:
    """Insert or replace multiple snapshots in a single executemany call.

    Returns the number of snapshots upserted.
    """
    if not snapshots:
        return 0

    conn.executemany(
        """
        INSERT OR REPLACE INTO snapshots
            (snapshot_id, repo_id, hostname, timestamp, paths, tags, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                s.snapshot_id,
                s.repo_id,
                s.hostname,
                s.timestamp,
                s.paths,
                s.tags,
                s.description,
            )
            for s in snapshots
        ],
    )
    if commit:
        conn.commit()
    return len(snapshots)


def list_snapshots(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
    *,
    include_forgotten: bool = False,
) -> list[Snapshot]:
    """List the snapshots, optionally of one repository.

    Snapshots no longer in the mirror (#824, ``FORGOTTEN_NOTE_PREFIX``)
    are left out unless *include_forgotten*: rustic cannot open them.
    """
    where = []
    params: list[str] = []
    if repo_id is not None:
        where.append("repo_id = ?")
        params.append(repo_id)
    if not include_forgotten:
        where.append("description NOT LIKE ? || '%'")
        params.append(FORGOTTEN_NOTE_PREFIX)
    sql = "SELECT * FROM snapshots"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY timestamp", params).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def get_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
) -> Snapshot | None:
    """Return a single snapshot by ID, or None if not found."""
    row = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_snapshot(row)


def delete_snapshots_for_repo(conn: sqlite3.Connection, repo_id: str) -> int:
    """Delete all snapshots belonging to *repo_id*. Returns count deleted."""
    cur = conn.execute(
        "DELETE FROM snapshots WHERE repo_id = ?", (repo_id,)
    )
    conn.commit()
    return cur.rowcount


def delete_blank_snapshots(conn: sqlite3.Connection) -> int:
    """Delete snapshot rows with an empty ``snapshot_id``; return the count.

    Scans before #536 misparsed rustic's grouped ``snapshots --json`` and
    stored every group as one row keyed ``''``.  No real snapshot has an
    empty id, so these rows are always junk.
    """
    cur = conn.execute("DELETE FROM snapshots WHERE snapshot_id = ''")
    conn.commit()
    return cur.rowcount


def _newest_disc_time(conn: sqlite3.Connection) -> float | None:
    """The newest time (julianday) any volume that is not DESTROYED was
    created, closed, verified or burned, or None when there is none.

    A disc carries the snapshot files that were in the mirror when its
    volume was staged, so a snapshot taken after this time is on no disc.
    The newest of all four times is used (not only ``created_at``): the
    metadata is copied after the volume row is created.
    """
    row = conn.execute(
        """
        SELECT MAX(t) FROM (
            SELECT julianday(created_at) AS t FROM volumes
                WHERE status != 'DESTROYED'
            UNION ALL SELECT julianday(closed_at) FROM volumes
                WHERE status != 'DESTROYED'
            UNION ALL SELECT julianday(verified_at) FROM volumes
                WHERE status != 'DESTROYED'
            UNION ALL SELECT julianday(vc.burn_date) FROM volume_copies vc
                JOIN volumes v ON v.volume_id = vc.volume_id
                WHERE v.status != 'DESTROYED' AND vc.status != 'DESTROYED'
        )
        """
    ).fetchone()
    return float(row[0]) if row is not None and row[0] is not None else None


def sync_forgotten_snapshots(
    conn: sqlite3.Connection,
    repo_id: str,
    listed_ids: set[str],
    *,
    commit: bool = True,
) -> tuple[list[str], list[str]]:
    """Reconcile *repo_id*'s snapshot rows with a complete mirror listing.

    A row whose snapshot is not in *listed_ids* (``rustic forget``):

    - is deleted when no disc can hold its snapshot file: no volume
      (other than DESTROYED ones) exists, or the snapshot was taken after
      the newest time of every such volume (``_newest_disc_time``);
    - is otherwise kept, and its ``description`` says it is no longer in
      the mirror (``FORGOTTEN_NOTE_PREFIX``).  A row whose time does not
      read is kept too.

    The disc test over-approximates: a volume staged after the snapshot
    was forgotten does not hold it, and a disc of an older volume holds
    only the snapshots whose packs were all on discs (#657).  It never
    deletes a row a disc may still need.

    Call it only with the listing of a scan that succeeded.  Returns
    (deleted snapshot ids, newly noted snapshot ids).
    """
    newest = _newest_disc_time(conn)
    note = _forgotten_note(utc_now()[:10])
    deleted: list[str] = []
    noted: list[str] = []
    rows = conn.execute(
        "SELECT snapshot_id, julianday(timestamp), description "
        "FROM snapshots WHERE repo_id = ? ORDER BY snapshot_id",
        (repo_id,),
    ).fetchall()
    for snapshot_id, taken, description in rows:
        if snapshot_id in listed_ids:
            continue
        on_no_disc = newest is None or (taken is not None and taken > newest)
        if on_no_disc:
            conn.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            )
            deleted.append(snapshot_id)
        elif not str(description or "").startswith(FORGOTTEN_NOTE_PREFIX):
            conn.execute(
                "UPDATE snapshots SET description = ? WHERE snapshot_id = ?",
                (note, snapshot_id),
            )
            noted.append(snapshot_id)
    if commit:
        conn.commit()
    return deleted, noted
