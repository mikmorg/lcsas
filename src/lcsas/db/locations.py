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
    """Register a new physical storage location."""
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
) -> Location:
    """Get or create a location (race-safe via INSERT OR IGNORE)."""
    conn.execute(
        "INSERT OR IGNORE INTO locations (name, description) VALUES (?, ?)",
        (name, description),
    )
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


def delete_location(conn: sqlite3.Connection, name: str) -> None:
    """Delete a location (fails if copies still reference it)."""
    conn.execute("DELETE FROM locations WHERE name = ?", (name,))
    conn.commit()
