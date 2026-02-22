"""CRUD operations for the snapshots table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Snapshot


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
            "SELECT snapshot_id, repo_id, hostname, timestamp, paths, tags, "
            "description FROM snapshots WHERE repo_id = ? ORDER BY timestamp",
            (repo_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT snapshot_id, repo_id, hostname, timestamp, paths, tags, "
            "description FROM snapshots ORDER BY timestamp",
        ).fetchall()

    return [
        Snapshot(
            snapshot_id=r[0],
            repo_id=r[1],
            hostname=r[2],
            timestamp=r[3],
            paths=r[4],
            tags=r[5],
            description=r[6],
        )
        for r in rows
    ]


def get_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
) -> Snapshot | None:
    """Return a single snapshot by ID, or None if not found."""
    row = conn.execute(
        "SELECT snapshot_id, repo_id, hostname, timestamp, paths, tags, "
        "description FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return Snapshot(
        snapshot_id=row[0],
        repo_id=row[1],
        hostname=row[2],
        timestamp=row[3],
        paths=row[4],
        tags=row[5],
        description=row[6],
    )


def delete_snapshots_for_repo(conn: sqlite3.Connection, repo_id: str) -> int:
    """Delete all snapshots belonging to *repo_id*. Returns count deleted."""
    cur = conn.execute(
        "DELETE FROM snapshots WHERE repo_id = ?", (repo_id,)
    )
    conn.commit()
    return cur.rowcount
