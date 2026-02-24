"""CRUD operations for the snapshots table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Snapshot


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
) -> list[Snapshot]:
    """List all snapshots, optionally filtered by repo_id."""
    if repo_id is not None:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE repo_id = ? ORDER BY timestamp",
            (repo_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM snapshots ORDER BY timestamp",
        ).fetchall()

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
