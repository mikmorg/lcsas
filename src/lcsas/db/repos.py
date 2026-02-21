"""CRUD operations for the repositories table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Repository


def _row_to_repo(row: sqlite3.Row) -> Repository:
    # created_at may be absent on catalogs from schema v2
    try:
        created_at = row["created_at"]
    except (IndexError, KeyError):
        created_at = ""
    return Repository(
        repo_id=row["repo_id"],
        name=row["name"],
        mirror_path=row["mirror_path"],
        encryption_key_id=row["encryption_key_id"],
        created_at=created_at,
    )


def register_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    name: str,
    mirror_path: str,
    encryption_key_id: str = "",
) -> Repository:
    """Insert a repository. Returns the created Repository object."""
    conn.execute(
        """INSERT INTO repositories (repo_id, name, mirror_path, encryption_key_id)
           VALUES (?, ?, ?, ?)""",
        (repo_id, name, mirror_path, encryption_key_id),
    )
    conn.commit()
    return get_repo(conn, repo_id)


def get_repo(conn: sqlite3.Connection, repo_id: str) -> Repository:
    """Fetch a repository by ID. Raises ValueError if not found."""
    row = conn.execute(
        "SELECT * FROM repositories WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Repository '{repo_id}' not found")
    return _row_to_repo(row)


def list_repos(conn: sqlite3.Connection) -> list[Repository]:
    """List all registered repositories."""
    rows = conn.execute("SELECT * FROM repositories ORDER BY name").fetchall()
    return [_row_to_repo(r) for r in rows]


def delete_repo(conn: sqlite3.Connection, repo_id: str) -> None:
    """Delete a repository from the catalog."""
    conn.execute("DELETE FROM repositories WHERE repo_id = ?", (repo_id,))
    conn.commit()
