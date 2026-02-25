"""CRUD operations for the packs table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Pack

# Conservative batch size to stay well below SQLite's SQLITE_MAX_VARIABLE_NUMBER
# (999 on old builds, 32 766 on newer). Using 900 gives headroom for extra params.
_SQLITE_BATCH = 900


def _row_to_pack(row: sqlite3.Row) -> Pack:
    return Pack(
        pack_id=row["pack_id"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        repo_id=row["repo_id"],
        is_pruned=bool(row["is_pruned"]),
        created_at=row["created_at"],
    )


def register_pack(
    conn: sqlite3.Connection,
    sha256: str,
    size_bytes: int,
    repo_id: str,
) -> Pack:
    """Insert a new pack and return the created Pack object.

    If a pack with the same sha256 already exists, returns the existing one.
    Uses INSERT OR IGNORE to avoid TOCTOU races.
    """
    conn.execute(
        "INSERT OR IGNORE INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
        (sha256, size_bytes, repo_id),
    )
    conn.commit()
    result = get_pack_by_sha256(conn, sha256)
    if result is None:
        raise RuntimeError(
            f"Pack {sha256} should exist after INSERT OR IGNORE but was not found. "
            "This indicates a database integrity issue."
        )
    return result


def get_pack_by_id(conn: sqlite3.Connection, pack_id: int) -> Pack:
    """Fetch a pack by primary key. Raises ValueError if not found."""
    row = conn.execute(
        "SELECT * FROM packs WHERE pack_id = ?", (pack_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Pack with id {pack_id} not found")
    return _row_to_pack(row)


def get_pack_by_sha256(conn: sqlite3.Connection, sha256: str) -> Pack | None:
    """Fetch a pack by its SHA-256 hash."""
    row = conn.execute(
        "SELECT * FROM packs WHERE sha256 = ?", (sha256,)
    ).fetchone()
    return _row_to_pack(row) if row else None


def mark_pruned(conn: sqlite3.Connection, pack_id: int) -> None:
    """Mark a pack as logically pruned (still on WORM media, but dead)."""
    conn.execute(
        "UPDATE packs SET is_pruned = 1 WHERE pack_id = ?", (pack_id,)
    )
    conn.commit()


def bulk_mark_pruned(conn: sqlite3.Connection, pack_ids: list[int]) -> int:
    """Mark multiple packs as pruned in a single transaction.

    Returns the number of packs updated.
    """
    if not pack_ids:
        return 0
    updated = 0
    for i in range(0, len(pack_ids), _SQLITE_BATCH):
        batch = pack_ids[i : i + _SQLITE_BATCH]
        placeholders = ",".join("?" * len(batch))
        cur = conn.execute(
            f"UPDATE packs SET is_pruned = 1 WHERE pack_id IN ({placeholders})"
            " AND is_pruned = 0",
            batch,
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def bulk_register(
    conn: sqlite3.Connection,
    packs: list[tuple[str, int, str]],
) -> list[Pack]:
    """Register multiple packs in a single transaction.

    Uses INSERT OR IGNORE + executemany for efficient bulk insertion
    without TOCTOU races.

    Args:
        packs: List of (sha256, size_bytes, repo_id) tuples.

    Returns:
        List of Pack objects (existing or newly created).
    """
    if not packs:
        return []
    conn.executemany(
        "INSERT OR IGNORE INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
        packs,
    )
    conn.commit()
    # Fetch all by sha256 in batches to avoid SQLite variable limit
    sha_list = [p[0] for p in packs]
    pack_map: dict[str, Pack] = {}
    for i in range(0, len(sha_list), _SQLITE_BATCH):
        batch = sha_list[i : i + _SQLITE_BATCH]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT * FROM packs WHERE sha256 IN ({ph})",
            batch,
        ).fetchall()
        for r in rows:
            pack_map[r["sha256"]] = _row_to_pack(r)
    return [pack_map[sha] for sha in sha_list]


def list_packs(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
    include_pruned: bool = False,
) -> list[Pack]:
    """List packs, optionally filtered by repo and prune status."""
    conditions: list[str] = []
    params: list[str | int] = []

    if repo_id is not None:
        conditions.append("repo_id = ?")
        params.append(repo_id)
    if not include_pruned:
        conditions.append("is_pruned = 0")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM packs {where} ORDER BY created_at", params
    ).fetchall()
    return [_row_to_pack(r) for r in rows]
