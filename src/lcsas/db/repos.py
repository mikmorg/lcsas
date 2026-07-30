"""CRUD operations for the repositories table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Repository

# Valid values for ``repositories.status`` (schema v10).
#
#   active   — the repo is live; its mirror is expected to be reachable and
#              `lcsas meta build` requires its Rustic keys to be bundled.
#   retired  — the mirror is gone for good.  The catalog row must stay
#              (its packs are on burned discs and `repo remove --force`
#              would destroy that pack/snapshot history), but the operator
#              has said so explicitly, so the meta-build key gate skips it.
#
# Enforced here rather than by a SQL CHECK: SQLite cannot retro-apply a
# CHECK via ALTER TABLE ADD COLUMN, so a constraint in the CREATE would
# exist on fresh v10 catalogs and be silently absent on migrated ones.
REPO_STATUS_ACTIVE = "active"
REPO_STATUS_RETIRED = "retired"
REPO_STATUSES: tuple[str, ...] = (REPO_STATUS_ACTIVE, REPO_STATUS_RETIRED)


def _row_to_repo(row: sqlite3.Row) -> Repository:
    # created_at may be absent on catalogs from schema v2
    try:
        created_at = row["created_at"]
    except (IndexError, KeyError):
        created_at = ""
    # status is absent on catalogs older than v10 (including read-only
    # on-disc catalogs, which are never migrated in place) — those repos
    # pre-date retirement and are therefore active.
    try:
        status = row["status"]
    except (IndexError, KeyError):
        status = REPO_STATUS_ACTIVE
    return Repository(
        repo_id=row["repo_id"],
        name=row["name"],
        mirror_path=row["mirror_path"],
        encryption_key_id=row["encryption_key_id"],
        created_at=created_at,
        status=status or REPO_STATUS_ACTIVE,
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


def set_repo_status(
    conn: sqlite3.Connection, repo_id: str, status: str
) -> Repository:
    """Set a repository's lifecycle status.  Returns the updated repo.

    Raises:
        ValueError: If *status* is not one of :data:`REPO_STATUSES`, or if
            the repository does not exist.
    """
    if status not in REPO_STATUSES:
        raise ValueError(
            f"Invalid repository status '{status}'. "
            f"Expected one of: {', '.join(REPO_STATUSES)}"
        )
    get_repo(conn, repo_id)  # raises ValueError when unknown
    conn.execute(
        "UPDATE repositories SET status = ? WHERE repo_id = ?",
        (status, repo_id),
    )
    conn.commit()
    return get_repo(conn, repo_id)


def delete_repo(conn: sqlite3.Connection, repo_id: str) -> None:
    """Delete a repository from the catalog.

    Raises:
        ValueError: If the repository has associated packs.  Delete or
            reassign them first, or use the *force* parameter.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM packs WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    if row and row[0] > 0:
        raise ValueError(
            f"Repository '{repo_id}' has {row[0]} associated pack(s). "
            "Remove all packs before deleting the repository."
        )
    conn.execute("DELETE FROM repositories WHERE repo_id = ?", (repo_id,))
    conn.commit()
