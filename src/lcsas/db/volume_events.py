"""CRUD operations for the volume_events table."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from lcsas.db.models import VolumeEvent


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
        event_date = datetime.now(UTC).isoformat()
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


def burn_evidence(conn: sqlite3.Connection, volume_id: int) -> str | None:
    """Why the volume's events say a good disc of it was burned, or None.

    The one check that ``session abort``, ``stage --clean`` (#737) and
    ``catalog reconcile --fix`` (#746) use before deleting a volume with
    no disc record: a volume it names was burned, and is never deleted.

    Only events that a never-burned volume, or one whose only disc
    failed verification (#483), cannot carry count:

    - ``VERIFY_PASS`` at a location: a disc was read back at that
      location.  An ISO verify records no location, so it does not
      count.  ``lcsas verify <label> --disc`` without ``--location``
      records none either; its default detail "Disc verify" counts.
    - ``BURN_RECEIPT_IMPORTED``, unless its receipt says the disc
      failed verification (``verify_passed`` false).
    - ``LOCATION_MOVE``: a recorded disc was moved.
    - ``VERIFY_FAIL_REBURN``: written only when an earlier disc of
      the volume was recorded.

    ``VERIFY_FAIL`` alone is not evidence: such a volume goes back
    to STAGING and is re-staged or aborted (BURN-05, #483).
    """
    rows = conn.execute(
        """SELECT event_type, location, detail FROM volume_events
           WHERE volume_id = ?
             AND event_type IN ('VERIFY_PASS', 'BURN_RECEIPT_IMPORTED',
                                'LOCATION_MOVE', 'VERIFY_FAIL_REBURN')
           ORDER BY julianday(event_date), event_id""",
        (volume_id,),
    ).fetchall()
    for event_type, location, detail in rows:
        where = f" at {location}" if location else ""
        if event_type == "VERIFY_PASS":
            if location or detail == "Disc verify":
                return f"a disc of it passed verification{where}"
        elif event_type == "BURN_RECEIPT_IMPORTED":
            try:
                receipt = json.loads(detail or "{}")
            except ValueError:
                receipt = {}
            if not (isinstance(receipt, dict)
                    and receipt.get("verify_passed") is False):
                return f"a burn receipt of it was imported{where}"
        elif event_type == "LOCATION_MOVE":
            return f"a disc of it was moved{where}"
        else:
            return f"a disc of it was burned again{where}"
    return None


# #758: the NOTE a catalog rebuild writes on a volume it brought in as
# STAGING/BURNING with no disc record and no burn evidence.  The prefix
# is what rebuild_burn_unknown() looks for; keep the two together.
REBUILD_UNKNOWN_NOTE_PREFIX = "Catalog rebuild: burn state unknown"


def rebuild_burn_unknown(conn: sqlite3.Connection, volume_id: int) -> bool:
    """True when nobody knows whether a disc of the volume was burned.

    A catalog rebuilt from discs knows a volume of a newer burn session
    only from the disc catalogs of discs staged after it, all copied
    before that volume was burned.  Unless its own disc was read
    (``db/rebuild.py`` then marks it BURNED), it comes back STAGING with
    no disc record and no events, which is how a never-burned volume
    looks.  The rebuild writes a ``NOTE`` starting with
    :data:`REBUILD_UNKNOWN_NOTE_PREFIX` on each such volume.

    True while that holds: the volume is STAGING or BURNING, has the
    note, no ``volume_copies`` row and no :func:`burn_evidence`.
    Recording its disc ends it.  ``session abort``, ``stage --clean``
    and ``catalog reconcile --fix`` never delete such a volume unless
    the owner confirms it was never burned (``--never-burned``).
    """
    row = conn.execute(
        """SELECT 1 FROM volumes v
           WHERE v.volume_id = ?
             AND v.status IN ('STAGING', 'BURNING')
             AND NOT EXISTS (SELECT 1 FROM volume_copies vc
                             WHERE vc.volume_id = v.volume_id)
             AND EXISTS (SELECT 1 FROM volume_events e
                         WHERE e.volume_id = v.volume_id
                           AND e.event_type = 'NOTE'
                           AND substr(e.detail, 1, ?) = ?)""",
        (volume_id, len(REBUILD_UNKNOWN_NOTE_PREFIX),
         REBUILD_UNKNOWN_NOTE_PREFIX),
    ).fetchone()
    return row is not None and burn_evidence(conn, volume_id) is None
