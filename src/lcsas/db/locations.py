"""CRUD operations for the locations table."""

from __future__ import annotations

import difflib
import sqlite3

from lcsas.db.models import Location


class UnknownLocationError(ValueError):
    """Raised when a location name does not match any registered row.

    Carries the offending name and an optional list of close matches so
    the CLI can render a helpful "did you mean …?" suggestion without
    re-implementing the similarity heuristic at each call site.
    """

    def __init__(self, name: str, suggestions: list[str] | None = None) -> None:
        self.name = name
        self.suggestions = suggestions or []
        msg = f"Unknown location '{name}'"
        if self.suggestions:
            msg += f" (did you mean: {', '.join(self.suggestions)}?)"
        msg += (
            ". Register it first with `lcsas location add <name>`, or pass "
            "`--create-location` to create it during burn."
        )
        super().__init__(msg)


def _row_to_location(row: sqlite3.Row) -> Location:
    return Location(
        name=row["name"],
        created_at=row["created_at"],
        description=row["description"],
    )


def create_location(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
) -> Location:
    """Register a new location."""
    conn.execute(
        "INSERT INTO locations (name, description) VALUES (?, ?)",
        (name, description),
    )
    conn.commit()
    return get_location(conn, name)


def get_location(conn: sqlite3.Connection, name: str) -> Location:
    """Get a location by name."""
    row = conn.execute(
        "SELECT * FROM locations WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Location '{name}' not found")
    return _row_to_location(row)


def ensure_location(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    *,
    commit: bool = True,
) -> Location:
    """Get or create a location (race-safe via INSERT OR IGNORE).

    ``commit=False`` leaves the insert in the caller's transaction, so a
    later failure rolls it back with everything else.
    """
    conn.execute(
        "INSERT OR IGNORE INTO locations (name, description) VALUES (?, ?)",
        (name, description),
    )
    if commit:
        conn.commit()
    return get_location(conn, name)


def resolve_location(
    conn: sqlite3.Connection,
    name: str,
    *,
    create: bool = False,
    description: str = "",
) -> Location:
    """Strictly resolve a location name, optionally creating it.

    Default behaviour rejects unknown names (preventing the silent
    auto-create bug where a typo like ``home-safe`` vs the real
    ``home_safe`` produces a phantom location row, see issue #19).

    Args:
        conn: open sqlite3 connection.
        name: the location name to look up.
        create: if True, create the location when missing (mirrors the
            ``ensure_location`` semantics but only when callers opt in).
        description: description used when ``create`` is True.

    Raises:
        UnknownLocationError: when the name is not registered and
            ``create`` is False. The exception carries up to three
            close-match suggestions computed via stdlib ``difflib``.
    """
    row = conn.execute(
        "SELECT * FROM locations WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        return _row_to_location(row)

    if create:
        return ensure_location(conn, name, description)

    existing = [r["name"] for r in conn.execute(
        "SELECT name FROM locations ORDER BY name"
    ).fetchall()]
    suggestions = difflib.get_close_matches(name, existing, n=3, cutoff=0.6)
    raise UnknownLocationError(name, suggestions)


def list_locations(conn: sqlite3.Connection) -> list[Location]:
    """List all registered locations."""
    rows = conn.execute(
        "SELECT * FROM locations ORDER BY name"
    ).fetchall()
    return [_row_to_location(r) for r in rows]


def location_references(conn: sqlite3.Connection, name: str) -> dict[str, int]:
    """Rows that name location *name*: disc records and audit events."""
    return {
        "discs": conn.execute(
            "SELECT COUNT(*) FROM volume_copies WHERE location = ?", (name,)
        ).fetchone()[0],
        "events": conn.execute(
            "SELECT COUNT(*) FROM volume_events WHERE location = ?", (name,)
        ).fetchone()[0],
    }


def delete_location(conn: sqlite3.Connection, name: str) -> None:
    """Delete location *name*.

    Raises ValueError if it doesn't exist, or if any disc record or audit
    event still names it: those rows are history, and deleting them to
    free the name would lose it (#491).  Move the discs with
    ``location move``, or rename the location instead.
    """
    get_location(conn, name)
    refs = location_references(conn, name)
    if refs["discs"] or refs["events"]:
        raise ValueError(
            f"Location '{name}' is still named by {refs['discs']} disc "
            f"record(s) and {refs['events']} audit event(s); move the discs "
            "with `lcsas location move`, or rename the location instead."
        )
    conn.execute("DELETE FROM locations WHERE name = ?", (name,))
    conn.commit()


def rename_location(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Rename location *old* to *new*, carrying every reference along (#491).

    ``volume_copies`` and ``volume_events`` refer to a location by name with
    no ON UPDATE CASCADE, so in one transaction with the foreign-key check
    deferred to commit: add *new* (keeping the description and creation
    time), repoint every reference, then drop *old*.  ``volumes.location``
    has no foreign key but the restore planner prefers volumes by it, so it
    is repointed too.  Returns the number of
    rows repointed.  Raises ValueError if *old* is missing or *new* exists.
    """
    loc = get_location(conn, old)
    if conn.execute(
        "SELECT 1 FROM locations WHERE name = ?", (new,)
    ).fetchone() is not None:
        raise ValueError(f"Location '{new}' already exists")
    conn.commit()  # start from a clean transaction boundary
    try:
        # Explicit BEGIN: the deferral lasts until the transaction ends.
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys = ON")
        conn.execute(
            "INSERT INTO locations (name, description, created_at) "
            "VALUES (?, ?, ?)",
            (new, loc.description, loc.created_at),
        )
        moved = 0
        for table in ("volume_copies", "volume_events", "volumes"):
            moved += conn.execute(
                f"UPDATE {table} SET location = ? WHERE location = ?",
                (new, old),
            ).rowcount
        conn.execute("DELETE FROM locations WHERE name = ?", (old,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return moved
