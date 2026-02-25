"""CRUD operations for the locations table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Location


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
