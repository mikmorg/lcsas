"""CRUD operations for the volume_events table."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import VolumeEvent
from lcsas.utils.timestamps import utc_now


def _row_to_event(row: sqlite3.Row) -> VolumeEvent:
    return VolumeEvent(
        event_id=row["event_id"],
        volume_id=row["volume_id"],
        event_type=row["event_type"],
        event_date=row["event_date"],
        location=row["location"],
        detail=row["detail"],
    )


# Valid event types — must match the CHECK constraint in schema.py
VALID_EVENT_TYPES = frozenset({
    "VERIFY_PASS",
    "VERIFY_FAIL",
    "VERIFY_FAIL_REBURN",
    "ECC_REPAIR",
    "LOCATION_MOVE",
    "CONDITION_CHECK",
    "NOTE",
    "BURN_RECEIPT_IMPORTED",
})


def add_event(
    conn: sqlite3.Connection,
    volume_id: int,
    event_type: str,
    location: str | None = None,
    detail: str = "",
    *,
    event_date: str | None = None,
    commit: bool = True,
) -> VolumeEvent:
    """Record a lifecycle event for a volume.

    Parameters
    ----------
    volume_id:
        The volume this event pertains to.
    event_type:
        One of VERIFY_PASS, VERIFY_FAIL, VERIFY_FAIL_REBURN, ECC_REPAIR,
        LOCATION_MOVE, CONDITION_CHECK, NOTE, BURN_RECEIPT_IMPORTED.
    location:
        Optional location associated with this event.
    detail:
        Free-text detail (e.g. JSON with failed pack list).
    event_date:
        ISO datetime string; defaults to now.  Stored in UTC ISO-8601
        (#730): a value with another offset or none (a receipt's burn
        date) is rewritten; one that does not parse is kept as given.
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"Invalid event_type {event_type!r}; "
            f"must be one of {sorted(VALID_EVENT_TYPES)}"
        )
    if event_date is None:
        event_date = utc_now()
    else:
        from lcsas.utils.timestamps import to_utc_iso

        event_date = to_utc_iso(event_date) or event_date

    cursor = conn.execute(
        """INSERT INTO volume_events (volume_id, event_type, event_date, location, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (volume_id, event_type, event_date, location, detail),
    )
    if commit:
        conn.commit()
    return get_event(conn, cursor.lastrowid)  # type: ignore[arg-type]


def get_event(conn: sqlite3.Connection, event_id: int) -> VolumeEvent:
    """Fetch a single event by ID."""
    row = conn.execute(
        "SELECT * FROM volume_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Volume event {event_id} not found")
    return _row_to_event(row)


def get_events_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    event_type: str | None = None,
) -> list[VolumeEvent]:
    """Return all events for a volume, optionally filtered by type.

    Results are ordered newest-first.
    """
    if event_type:
        rows = conn.execute(
            """SELECT * FROM volume_events
               WHERE volume_id = ? AND event_type = ?
               ORDER BY julianday(event_date) DESC, event_id DESC""",
            (volume_id, event_type),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM volume_events
               WHERE volume_id = ?
               ORDER BY julianday(event_date) DESC, event_id DESC""",
            (volume_id,),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def get_latest_event(
    conn: sqlite3.Connection,
    volume_id: int,
    event_type: str | None = None,
) -> VolumeEvent | None:
    """Return the most recent event for a volume (optionally by type)."""
    if event_type:
        row = conn.execute(
            """SELECT * FROM volume_events
               WHERE volume_id = ? AND event_type = ?
               ORDER BY julianday(event_date) DESC, event_id DESC LIMIT 1""",
            (volume_id, event_type),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM volume_events
               WHERE volume_id = ?
               ORDER BY julianday(event_date) DESC, event_id DESC LIMIT 1""",
            (volume_id,),
        ).fetchone()
    return _row_to_event(row) if row else None


# #758: the NOTE a catalog rebuild writes on a volume it brought in as
# STAGING/BURNING with no disc record and no burn evidence.  The prefix
# is what lcsas.db.lifecycle.VolumeLifecycle.burn_unknown() looks for.
REBUILD_UNKNOWN_NOTE_PREFIX = "Catalog rebuild: burn state unknown"


