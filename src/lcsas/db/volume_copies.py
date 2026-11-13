"""CRUD operations for the volume_copies table."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from lcsas.db.models import VolumeCopy

_logger = logging.getLogger(__name__)


def _row_to_copy(row: sqlite3.Row) -> VolumeCopy:
    # iso_sha256, last_verified_at, media_serial may be absent on v3 catalogs
    try:
        iso_sha256 = row["iso_sha256"]
    except (IndexError, KeyError):
        iso_sha256 = None
    try:
        last_verified_at = row["last_verified_at"]
    except (IndexError, KeyError):
        last_verified_at = None
    try:
        media_serial = row["media_serial"]
    except (IndexError, KeyError):
        media_serial = ""
    try:
        iso_size_bytes = row["iso_size_bytes"]
    except (IndexError, KeyError):
        iso_size_bytes = None
    return VolumeCopy(
        id=row["id"],
        volume_id=row["volume_id"],
        location=row["location"],
        status=row["status"],
        burn_date=row["burn_date"],
        notes=row["notes"],
        iso_sha256=iso_sha256,
        last_verified_at=last_verified_at,
        media_serial=media_serial,
        iso_size_bytes=iso_size_bytes,
    )


def add_volume_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    burn_date: str | None = None,
    notes: str = "",
    *,
    iso_sha256: str | None = None,
    iso_size_bytes: int | None = None,
    last_verified_at: str | None = None,
    media_serial: str = "",
    verified: bool | None = None,
    commit: bool = True,
) -> VolumeCopy:
    """Record a disc of a volume at a location.

    If a disc is already recorded at this location (re-burn), the burn_date,
    notes, and verification evidence are updated in-place: the row
    describes the NEW disc, so a stale ``last_verified_at`` from the
    replaced disc must not survive the re-burn.

    Evidence is never destroyed by an *absent* value (FMA-04): a caller
    that omits ``iso_sha256`` / ``iso_size_bytes`` / ``media_serial``
    must not blank what a previous verified burn recorded — that hash is
    what the portable SHA-256 verify fallback depends on.  Provided
    (non-NULL / non-empty) values still replace, because they describe
    the new disc.

    A lost volume (DEPRECATED because its last in-service disc went)
    that gets a disc recorded here is back in service (#726): VERIFIED
    when the disc was verified, else BURNED (:func:`_revive_lost_volume`).
    *verified* says whether it was; None reads it from *last_verified_at*.
    A volume deprecated by consolidation stays DEPRECATED.
    """
    from lcsas.utils.timestamps import to_utc_iso

    if burn_date is None:
        burn_date = datetime.now(UTC).isoformat()
    # #730: one format (UTC ISO-8601) for new values; a receipt's burn
    # date may carry another offset.  Unreadable text is kept as given.
    burn_date = to_utc_iso(burn_date) or burn_date
    if last_verified_at is not None:
        last_verified_at = to_utc_iso(last_verified_at) or last_verified_at
    conn.execute(
        """INSERT INTO volume_copies
               (volume_id, location, burn_date, notes, iso_sha256,
                iso_size_bytes, last_verified_at, media_serial)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(volume_id, location) DO UPDATE SET
               burn_date        = excluded.burn_date,
               notes            = excluded.notes,
               status           = 'ACTIVE',
               iso_sha256       = COALESCE(excluded.iso_sha256,
                                           volume_copies.iso_sha256),
               iso_size_bytes   = COALESCE(excluded.iso_size_bytes,
                                           volume_copies.iso_size_bytes),
               last_verified_at = excluded.last_verified_at,
               media_serial     = CASE WHEN excluded.media_serial != ''
                                       THEN excluded.media_serial
                                       ELSE volume_copies.media_serial END""",
        (volume_id, location, burn_date, notes, iso_sha256,
         iso_size_bytes, last_verified_at, media_serial),
    )
    _revive_lost_volume(
        conn, volume_id, location,
        verified=(last_verified_at is not None if verified is None
                  else verified),
    )
    if commit:
        conn.commit()
    # Fetch by (volume_id, location) instead of lastrowid (unreliable after UPSERT on SQLite < 3.35)
    row = conn.execute(
        "SELECT * FROM volume_copies WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'",
        (volume_id, location),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"Failed to insert/update the disc record for volume {volume_id} at {location}"
        )
    return _row_to_copy(row)


def get_copy_at_location(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
) -> VolumeCopy | None:
    """The disc record of a volume at a location, whatever its status, or None.

    There is at most one per (volume, location): the upsert in
    :func:`add_volume_copy` reuses the row.
    """
    row = conn.execute(
        "SELECT * FROM volume_copies WHERE volume_id = ? AND location = ?",
        (volume_id, location),
    ).fetchone()
    return _row_to_copy(row) if row is not None else None


def get_disc_retired_at(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
) -> str | None:
    """When the disc at *location* was last recorded deprecated, destroyed
    or lost (the event date of the newest NOTE written by
    ``_record_disc_fate``), or None if there is no such record (#666)."""
    prefix = _disc_fate_prefix(location)
    for row in conn.execute(
        """SELECT event_date, detail FROM volume_events
           WHERE volume_id = ? AND event_type = 'NOTE' AND location = ?
           ORDER BY julianday(event_date) DESC, event_id DESC""",
        (volume_id, location),
    ):
        if str(row["detail"]).startswith(prefix):
            return str(row["event_date"])
    return None


def get_copies_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    active_only: bool = True,
) -> list[VolumeCopy]:
    """Get all discs recorded for a specific volume."""
    if active_only:
        rows = conn.execute(
            "SELECT * FROM volume_copies WHERE volume_id = ? AND status = 'ACTIVE'",
            (volume_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM volume_copies WHERE volume_id = ?",
            (volume_id,),
        ).fetchall()
    return [_row_to_copy(r) for r in rows]


def get_iso_sha256_for_label(
    conn: sqlite3.Connection,
    volume_label: str,
) -> str | None:
    """Return the recorded ISO SHA-256 for a volume label, or None.

    Phase 21.3 helper for the portable verifier.  Looks first at
    ``volume_copies`` (one row per location, written at burn time when
    a disc lands at a location), then falls back to the most recent
    ``session_volumes`` row for the same volume (covers the staged-but-
    not-yet-burned-to-a-location case).

    Returns the first non-null hash found.  Returns ``None`` if the
    volume label is unknown OR none of its records carry a hash
    (older v3 catalogs, or discs recorded before Phase 13).
    """
    from lcsas.db.volumes import get_volume_by_label

    vol = get_volume_by_label(conn, volume_label)
    if vol is None:
        return None

    # Prefer volume_copies — per-location, written at burn-to-disc time.
    for copy in get_copies_for_volume(conn, vol.volume_id, active_only=False):
        if copy.iso_sha256:
            return copy.iso_sha256

    # Fallback: session_volumes — written at ISO mastering time.  Pick
    # the latest session for this volume to favor the most recent burn.
    row = conn.execute(
        """SELECT sv.iso_sha256
           FROM session_volumes sv
           JOIN burn_sessions bs USING (session_id)
           WHERE sv.volume_id = ?
             AND sv.iso_sha256 IS NOT NULL
           ORDER BY julianday(bs.created_at) DESC
           LIMIT 1""",
        (vol.volume_id,),
    ).fetchone()
    if row and row["iso_sha256"]:
        return str(row["iso_sha256"])
    return None


def move_volume_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    from_location: str,
    to_location: str,
) -> None:
    """Record a physical disc moving from one location to another.

    Also emits a LOCATION_MOVE row in ``volume_events`` so the audit trail
    captures every physical move (issue #16). The update and the event
    insert share a single transaction so they commit atomically.
    """
    now = datetime.now(UTC).isoformat()
    try:
        result = conn.execute(
            """UPDATE volume_copies
               SET location = ?,
                   notes = COALESCE(notes, '') || 'Moved from ' || ? || ' on ' || ? || char(10)
               WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'""",
            (to_location, from_location, now, volume_id, from_location),
        )
    except sqlite3.IntegrityError:
        raise ValueError(
            f"A disc of volume {volume_id} is already recorded at '{to_location}'. "
            f"The disc cannot be moved there without first removing that disc record."
        ) from None
    if result.rowcount == 0:
        raise ValueError(
            f"No in-service disc of volume {volume_id} at '{from_location}'"
        )
    # Audit trail: record the move as a LOCATION_MOVE event. The new location
    # goes in the dedicated `location` column; from/to are also serialised
    # into `detail` so the original location is recoverable from the event row
    # alone. Same connection -> same transaction as the UPDATE above.
    detail = json.dumps(
        {"from_location": from_location, "to_location": to_location}
    )
    conn.execute(
        """INSERT INTO volume_events (volume_id, event_type, event_date, location, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (volume_id, "LOCATION_MOVE", now, to_location, detail),
    )
    conn.commit()


def touch_last_verified(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    *,
    commit: bool = True,
) -> bool:
    """Stamp ``last_verified_at = now`` on the ACTIVE disc at a location.

    Returns True if a row was updated (BURN-04: re-verification evidence
    for the disc-rot re-check cadence).

    A lost volume whose disc was recorded again before #726 is still
    DEPRECATED with this ACTIVE disc; the disc just read puts it back in
    service, VERIFIED, as recording it would now (#752,
    :func:`_revive_lost_volume`).  A volume deprecated by consolidation
    stays DEPRECATED.
    """
    now = datetime.now(UTC).isoformat()
    result = conn.execute(
        """UPDATE volume_copies SET last_verified_at = ?
           WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'""",
        (now, volume_id, location),
    )
    if result.rowcount > 0:
        _revive_lost_volume(conn, volume_id, location, verified=True)
    if commit:
        conn.commit()
    return result.rowcount > 0


def find_stale_copies(
    conn: sqlite3.Connection,
    older_than_days: int = 365,
    *,
    now: datetime | None = None,
) -> list[tuple[str, str, str | None]]:
    """ACTIVE discs whose ``last_verified_at`` is NULL or too old.

    Disc-rot re-verification cadence (FMA-05): returns
    ``(label, location, last_verified_at)`` tuples, never-verified
    (NULL) first, then oldest first.  Discs of DEPRECATED/DESTROYED
    volumes are excluded — those discs are already out of service.

    ``last_verified_at`` is compared and ordered as an instant
    (``julianday``), not as text: rows written before #730 or imported
    from receipts may carry another text form or offset.
    """
    if now is None:
        now = datetime.now(UTC)
    cutoff = (now - timedelta(days=older_than_days)).isoformat()
    rows = conn.execute(
        """SELECT v.label, vc.location, vc.last_verified_at
           FROM volume_copies vc JOIN volumes v USING (volume_id)
           WHERE vc.status = 'ACTIVE'
             AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
             AND (julianday(vc.last_verified_at) IS NULL
                  OR julianday(vc.last_verified_at) < julianday(?))
           ORDER BY vc.last_verified_at IS NOT NULL,
                    julianday(vc.last_verified_at), v.label, vc.location""",
        (cutoff,),
    ).fetchall()
    return [
        (r["label"], r["location"], r["last_verified_at"]) for r in rows
    ]


def _demote_volume_if_last_copy_lost(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    action: str,
) -> bool:
    """Record the volume as DEPRECATED (lost) when its last ACTIVE disc is gone.

    A volume with no disc left in service is a lost volume.  There is no
    LOST status in the schema (Q33, ADR 0003): it is recorded as
    DEPRECATED plus the NOTE event written here, and owner displays show
    it as LOST (``lcsas.db.queries.get_lost_volume_ids``).

    Replica truth (BURN-10): a volume is a live replica iff it has >=1
    ACTIVE disc — except a volume with zero ``volume_copies`` rows at all,
    which counts by status alone (legacy).  A volume whose disc rows exist
    but none are ACTIVE must therefore lose its durable status, or the
    redundancy report and deprecation guard keep treating a destroyed
    disc as a valid replica.

    Direct UPDATE rather than ``update_status``: DEPRECATED is not a
    legal transition from every durable state (e.g. BURNED), and the
    deprecation-safety guard must not veto recording physical reality.
    Already-DEPRECATED/DESTROYED volumes are left alone (never resurrect
    a DESTROYED volume).  Shares the caller's transaction; no commit.
    """
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END), 0)
                      AS active
           FROM volume_copies WHERE volume_id = ?""",
        (volume_id,),
    ).fetchone()
    if row["total"] == 0 or row["active"] > 0:
        return False
    result = conn.execute(
        """UPDATE volumes SET status = 'DEPRECATED'
           WHERE volume_id = ? AND status NOT IN ('DEPRECATED', 'DESTROYED')""",
        (volume_id,),
    )
    if result.rowcount == 0:
        return False
    from lcsas.constants import AUTO_DEPRECATED_NOTE_PREFIX
    from lcsas.db.volume_events import add_event
    # The prefix is what lcsas.db.queries.get_lost_volume_ids matches to
    # tell a lost volume from one deprecated by consolidation (ADR 0003).
    add_event(
        conn, volume_id, "NOTE",
        location=location,
        detail=(
            f"{AUTO_DEPRECATED_NOTE_PREFIX} last in-service disc {action} "
            f"at {location}"
        ),
        commit=False,
    )
    return True


def _revive_lost_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    *,
    verified: bool,
) -> str | None:
    """Put a lost volume back in service when a disc of it is recorded (#726).

    The reverse of :func:`_demote_volume_if_last_copy_lost`: a volume
    DEPRECATED because its last in-service disc went (lost, ADR 0003)
    that now has an ACTIVE disc again becomes VERIFIED when that disc
    was verified, else BURNED, as a first disc would.  Then the stage
    pool, the redundancy report and the deprecation guard count it again.
    A volume deprecated by consolidation is left alone
    (``lcsas.db.queries.deprecated_by_loss``).

    Direct UPDATE rather than ``update_status``: DEPRECATED → VERIFIED is
    not a general transition, only this one.  The NOTE says why.  Shares
    the caller's transaction; no commit.  Returns the new status, or None
    when nothing changed.
    """
    from lcsas.db.queries import deprecated_by_loss

    if not deprecated_by_loss(conn, volume_id):
        return None
    status = "VERIFIED" if verified else "BURNED"
    now = datetime.now(UTC).isoformat()
    if verified:
        conn.execute(
            "UPDATE volumes SET status = ?, verified_at = ? WHERE volume_id = ?",
            (status, now, volume_id),
        )
    else:
        conn.execute(
            "UPDATE volumes SET status = ? WHERE volume_id = ?",
            (status, volume_id),
        )
    from lcsas.db.volume_events import add_event
    add_event(
        conn, volume_id, "NOTE",
        location=location,
        detail=(
            f"Status changed: DEPRECATED → {status}; this lost volume has "
            f"a disc in service again at {location}"
        ),
        commit=False,
    )
    label = conn.execute(
        "SELECT label FROM volumes WHERE volume_id = ?", (volume_id,),
    ).fetchone()[0]
    _logger.info(
        "%s was a lost volume; with its disc at %s it is in service "
        "again (%s).", label, location, status,
    )
    return status


def _record_disc_fate(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    fate: str,
) -> None:
    """Write the per-disc NOTE event for ``lcsas copy deprecate/destroy``.

    One event per call, last disc or not, so the volume's events always
    say what happened to each disc.  A lost disc and a destroyed disc
    share the DESTROYED disc status; only this note tells them apart
    (Q38).  Shares the caller's transaction; no commit.
    """
    from lcsas.db.volume_events import add_event
    add_event(
        conn, volume_id, "NOTE",
        location=location,
        detail=f"{_disc_fate_prefix(location)}{fate}",
        commit=False,
    )


def _disc_fate_prefix(location: str) -> str:
    """The start of every per-disc fate NOTE (see get_disc_retired_at)."""
    return f"disc at {location} recorded as "


def deprecate_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
) -> bool:
    """Mark a disc as deprecated (e.g. damaged).

    Raises ValueError if no ACTIVE disc is recorded at the location.
    Returns True when this was the volume's last ACTIVE disc and the volume
    itself was auto-demoted to DEPRECATED (BURN-10).
    """
    result = conn.execute(
        """UPDATE volume_copies SET status = 'DEPRECATED'
           WHERE volume_id = ? AND location = ? AND status = 'ACTIVE'""",
        (volume_id, location),
    )
    if result.rowcount == 0:
        raise ValueError(
            f"No in-service disc of volume {volume_id} at '{location}'"
        )
    _record_disc_fate(
        conn, volume_id, location, "deprecated (out of service)"
    )
    demoted = _demote_volume_if_last_copy_lost(
        conn, volume_id, location, "deprecated"
    )
    conn.commit()
    return demoted


def destroy_copy(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    *,
    lost: bool = False,
) -> bool:
    """Mark a disc as destroyed, or with *lost* as lost.

    Both record the disc status DESTROYED (no schema change, Q38); the
    per-disc NOTE event says "destroyed" or "lost (whereabouts unknown)",
    and so does the volume's auto-deprecation note when this was its last
    in-service disc.

    Raises ValueError if no disc is recorded at the location.  Returns
    True when this was the volume's last ACTIVE disc and the volume
    itself was auto-demoted to DEPRECATED (BURN-10): it is now a lost
    volume.
    """
    result = conn.execute(
        """UPDATE volume_copies SET status = 'DESTROYED'
           WHERE volume_id = ? AND location = ?""",
        (volume_id, location),
    )
    if result.rowcount == 0:
        raise ValueError(
            f"No disc of volume {volume_id} recorded at '{location}'"
        )
    action = "lost" if lost else "destroyed"
    _record_disc_fate(
        conn, volume_id, location,
        "lost (whereabouts unknown)" if lost else "destroyed",
    )
    demoted = _demote_volume_if_last_copy_lost(
        conn, volume_id, location, action
    )
    conn.commit()
    return demoted
