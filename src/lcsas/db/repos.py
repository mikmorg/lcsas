"""CRUD operations for the repositories table."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from lcsas.db.models import Repository

# Valid values for ``repositories.status`` (schema v10).
#
#   active   — the repository is live; its mirror is expected to be
#              reachable and `lcsas meta build` requires its Rustic keys to
#              be bundled.
#   retired  — the mirror is gone for good.  The catalog row must stay
#              (its packs are on burned discs, so `repo remove` refuses
#              it, #962), but the owner
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
    # disc catalogs, which are never migrated in place) — those
    # repositories pre-date retirement and are therefore active.
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
    from lcsas.utils.timestamps import utc_now

    conn.execute(
        """INSERT INTO repositories (repo_id, name, mirror_path,
                                     encryption_key_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (repo_id, name, mirror_path, encryption_key_id, utc_now()),
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


def get_repo_by_name(conn: sqlite3.Connection, name: str) -> Repository | None:
    """Fetch a repository by its name (what ``--repo`` takes), or None.

    ``repo add`` gives each repository a UUID repo_id, so the name the
    owner types is not the ID the catalog keys on.  The name column has no
    UNIQUE constraint, so two rows can share a name: that raises
    ValueError rather than guessing which one was meant.
    """
    rows = conn.execute(
        "SELECT * FROM repositories WHERE name = ? ORDER BY repo_id", (name,)
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        ids = ", ".join(r["repo_id"] for r in rows)
        raise ValueError(
            f"More than one repository in the catalog is named '{name}' "
            f"(IDs: {ids})."
        )
    return _row_to_repo(rows[0])


def refresh_mirror_path(
    conn: sqlite3.Connection, repo: Repository, mirror_path: str | Path,
) -> bool:
    """Record *mirror_path* as where *repo*'s mirror is, when the catalog
    says otherwise (#958).  Returns whether it changed.

    The config owns a mirror's path; the catalog column is the last path
    ``lcsas scan`` saw, kept for the commands that run with no config.
    """
    if repo.mirror_path and same_mirror_path(repo.mirror_path, mirror_path):
        return False
    conn.execute(
        "UPDATE repositories SET mirror_path = ? WHERE repo_id = ?",
        (str(mirror_path), repo.repo_id),
    )
    conn.commit()
    return True


def same_mirror_path(a: str | Path, b: str | Path) -> bool:
    """Whether two mirror paths name the same directory, compared lexically.

    No filesystem access: a stale mount must not hang a lookup (#427).
    """
    return os.path.normpath(str(a)) == os.path.normpath(str(b))


def resolve_repo(
    conn: sqlite3.Connection,
    ref: str,
    mirror_path: str | Path | None = None,
) -> Repository | None:
    """The catalog repository an owner means by *ref* (#687).

    The ONE resolver every command uses for a repository it is given.
    *ref* is usually a ``[repos.<name>]`` config key; *mirror_path* is that
    key's configured mirror, when there is a config.  Order:

    1. the catalog repository with that mirror path — the config key may
       differ from the catalog name (``repo add`` takes any name, and a
       config can be renamed);
    2. the repository named *ref*;
    3. the repository whose catalog ID is *ref*;
    4. the one repository named *ref* ignoring case.

    Returns None when nothing matches.  Raises ValueError when a step
    matches more than one repository rather than guessing (#685).
    """
    repos = list_repos(conn)
    if mirror_path is not None:
        by_path = [r for r in repos if same_mirror_path(r.mirror_path, mirror_path)]
        if len(by_path) > 1:
            # Only a catalog from before #685 can hold these.  The name may
            # still single one out; two of that name too is the name's error.
            named = [r for r in by_path if r.name == ref]
            if len(named) == 1:
                return named[0]
            if named:
                get_repo_by_name(conn, ref)  # raises, naming both
        if len(by_path) > 1:
            names = ", ".join(f"'{r.name}' ({r.repo_id})" for r in by_path)
            raise ValueError(
                f"More than one repository in the catalog has the mirror "
                f"path {mirror_path}: {names}."
            )
        if by_path:
            return by_path[0]
    repo = get_repo_by_name(conn, ref)  # raises ValueError when ambiguous
    if repo is not None:
        return repo
    by_id = [r for r in repos if r.repo_id == ref]
    if by_id:
        return by_id[0]
    matches = [r for r in repos if r.name.lower() == ref.lower()]
    if len(matches) > 1:
        raise ValueError(
            f"'{ref}' matches {len(matches)} repositories; use its catalog ID "
            "(`lcsas repo list`)."
        )
    return matches[0] if matches else None


def list_repos(conn: sqlite3.Connection) -> list[Repository]:
    """List all registered repositories."""
    rows = conn.execute("SELECT * FROM repositories ORDER BY name").fetchall()
    return [_row_to_repo(r) for r in rows]


def set_repo_status(
    conn: sqlite3.Connection, repo_id: str, status: str
) -> Repository:
    """Set a repository's lifecycle status.  Returns the updated repository.

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


class RetiredSet:
    """A retired backup set and the disc an heir starts it from (#959)."""

    def __init__(self, name: str, last_label: str | None,
                 last_date: str | None) -> None:
        self.name = name
        #: The highest-numbered burned volume holding a pack of the set
        #: (None: none of its packs was ever burned).
        self.last_label = last_label
        #: The date (YYYY-MM-DD) that volume was prepared.
        self.last_date = last_date

    def sentence(self) -> str:
        """The one sentence the discs, the estate card and `repo retire`
        print.  The catalog records no retirement date, so the date is
        that of the set's last disc, and the sentence says "after"."""
        if self.last_label is None:
            return (f"{self.name}: retired.  None of its backups is on a "
                    "disc.")
        return (f"{self.name}: retired after {self.last_date}.  Its backups "
                f"are on the discs up to {self.last_label}.  To restore it, "
                'use that disc as the "newest data disc".')


def retired_backup_sets(conn: sqlite3.Connection) -> list[RetiredSet]:
    """Each retired repository, by name, with its last burned disc (#959).

    "Burned" is a volume that has left STAGING/BURNING and was not
    DESTROYED; the last one is the highest label number (the number at
    the end of the label, as the heir ranks discs).
    """
    from lcsas.db.label_mark import label_seq

    out: list[RetiredSet] = []
    for repo in sorted(list_repos(conn), key=lambda r: r.name):
        if repo.status != REPO_STATUS_RETIRED:
            continue
        rows = conn.execute(
            """SELECT DISTINCT v.label, v.created_at FROM volumes v
               JOIN volume_packs vp ON vp.volume_id = v.volume_id
               JOIN packs p ON p.pack_id = vp.pack_id
               WHERE p.repo_id = ?
                 AND v.status NOT IN ('STAGING', 'BURNING', 'DESTROYED')""",
            (repo.repo_id,),
        ).fetchall()
        if not rows:
            out.append(RetiredSet(repo.name, None, None))
            continue
        label, created = max(
            ((str(r[0]), str(r[1] or "")) for r in rows),
            key=lambda lc: (label_seq(lc[0]) or 0, lc[0]),
        )
        out.append(RetiredSet(repo.name, label, created[:10]))
    return out
