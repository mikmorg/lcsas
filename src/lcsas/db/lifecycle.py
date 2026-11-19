"""Volume lifecycle: every status change of a volume, by event (#892).

A caller says what happened (a burn started, a disc check passed, the
last disc was lost) and :class:`VolumeLifecycle` decides the status
change, writes it with its ``Status changed`` note, and records the disc
and the volume events that belong to that event.  The status graph, the
burn-evidence rule and the rule for which volumes may be deleted live
here, so a rule change is made in one place.  The table in
``tests/unit/test_volume_lifecycle_table.py`` (#891) pins what every event
does from every start state.

No method commits unless its docstring says so: its writes share the
caller's transaction, as the calls they replace did.

A lost volume is DEPRECATED plus an ``auto-deprecated:`` note; there is no
LOST status (ADR 0003).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from lcsas.constants import (
    AUTO_DEPRECATED_NOTE_PREFIX,
    STATUS_BURNED,
    STATUS_BURNING,
    STATUS_CONSOLIDATING,
    STATUS_DEPRECATED,
    STATUS_STAGING,
    STATUS_VERIFIED,
)
from lcsas.db.models import Volume
from lcsas.db.volume_events import REBUILD_UNKNOWN_NOTE_PREFIX, add_event
from lcsas.db.volumes import (
    check_deprecation_safe,
    get_volume_by_id,
    mark_closed,
)
from lcsas.utils.timestamps import utc_now

_logger = logging.getLogger(__name__)


# The status graph: each key maps to the set of statuses a volume may
# move to from that one.  Private: callers send events, and the events
# below are the only writers of volumes.status (#892).
#
# DESTROYED is not in it (#907): no command writes the volume-wide
# DESTROYED status ("destroy" is the disc-level 'copy destroy').  It stays
# a valid value in the volumes.status CHECK constraint, which needs no
# migration: the tier-1 reader filters on it (db/schema.py
# TIER1_REQUIRED_VOLUME_STATUS), and an old catalog, or a catalog rebuilt
# from an old disc catalog, may still hold it.  A volume that is DESTROYED
# has no key here, so _update_status refuses every change of it.
_TRANSITIONS: dict[str, set[str]] = {
    # A never-burned volume is deleted or staged again, never deprecated
    # (#905): no command deletes a DEPRECATED volume, so it would stay in
    # the catalog for good.
    STATUS_STAGING:         {STATUS_BURNING},
    STATUS_BURNING:         {STATUS_BURNED, STATUS_VERIFIED, STATUS_STAGING},
    # A BURNED volume never goes back to BURNING or STAGING (#810): a
    # disc of it exists (a catalog rebuilt from discs records the newest
    # ones as BURNED with no disc row), and STAGING would let 'session
    # abort' or 'reconcile --fix' delete it.  'burn --session' burns a
    # BURNED volume without leaving BURNED, then makes it VERIFIED.
    STATUS_BURNED:          {STATUS_VERIFIED},
    STATUS_VERIFIED:        {STATUS_DEPRECATED, STATUS_CONSOLIDATING},
    STATUS_CONSOLIDATING:   {STATUS_DEPRECATED, STATUS_VERIFIED},
    STATUS_DEPRECATED:      set(),
}


def _update_status(
    conn: sqlite3.Connection,
    volume_id: int,
    status: str,
    *,
    commit: bool = True,
    force: bool = False,
) -> None:
    """Update the status of a volume.

    Enforces valid state transitions unless *force* is ``True``.
    Automatically sets ``verified_at`` when status transitions to VERIFIED.
    """
    volume = get_volume_by_id(conn, volume_id)
    current = volume.status

    if not force:
        allowed = _TRANSITIONS.get(current, set())
        if status not in allowed:
            raise ValueError(
                f"Invalid status transition for volume {volume_id}: "
                f"{current} → {status} (allowed: {sorted(allowed)})"
            )
        # Safety check: refuse DEPRECATED if packs would become unreplicated.
        # Use a savepoint for write-lock protection (nests safely in outer transactions).
        if status == "DEPRECATED":
            # Outside a transaction a SAVEPOINT opens one and its RELEASE
            # commits it, so the change would be committed at once, before
            # the caller's commit (or rollback: 'consolidate --deprecate'
            # of several volumes is all or nothing, #905).
            if not conn.in_transaction:
                conn.execute("BEGIN")
            conn.execute("SAVEPOINT deprecate_check")
            try:
                at_risk = check_deprecation_safe(conn, volume_id)
                if at_risk:
                    conn.execute("ROLLBACK TO deprecate_check")
                    raise ValueError(
                        f"Cannot deprecate volume {volume.label}: "
                        f"{len(at_risk)} of its pack(s) are on no other "
                        f"in-service volume; burn and verify the volume(s) "
                        f"they were consolidated onto first."
                    )
                conn.execute(
                    "UPDATE volumes SET status = ? WHERE volume_id = ?",
                    (status, volume_id),
                )
                conn.execute("RELEASE deprecate_check")
                add_event(
                    conn, volume_id, "NOTE",
                    detail=f"Status changed: {current} → {status}",
                    commit=False,
                )
                if commit:
                    conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK TO deprecate_check")
                raise
            return
    else:
        _logger.warning(
            "Forced status change for volume %d → %s (bypassing transition rules)",
            volume_id, status,
        )

    if status == "VERIFIED":
        now = utc_now()
        conn.execute(
            "UPDATE volumes SET status = ?, verified_at = ? WHERE volume_id = ?",
            (status, now, volume_id),
        )
    else:
        conn.execute(
            "UPDATE volumes SET status = ? WHERE volume_id = ?",
            (status, volume_id),
        )

    add_event(
        conn, volume_id, "NOTE",
        detail=f"Status changed: {current} → {status}",
        commit=False,
    )

    if commit:
        conn.commit()


def _status_before_burning(conn: sqlite3.Connection, volume_id: int) -> str:
    """The status a BURNING volume had before its burn started (#810).

    A burn killed outright (power loss, ``kill -9``) leaves its volume
    BURNING; the status it came from is only in the NOTE that
    :func:`_update_status` wrote ("Status changed: X → BURNING").
    Returns ``BURNED`` when the newest such note says it was BURNED (an
    older LCSAS moved a BURNED volume to BURNING for its burn), else
    ``STAGING``, also when there is no note.
    """
    row = conn.execute(
        """SELECT detail FROM volume_events
           WHERE volume_id = ? AND event_type = 'NOTE'
             AND detail LIKE 'Status changed: % → BURNING'
           ORDER BY event_id DESC LIMIT 1""",
        (volume_id,),
    ).fetchone()
    if row is not None and str(row[0]).startswith(
            f"Status changed: {STATUS_BURNED} →"):
        return STATUS_BURNED
    return STATUS_STAGING


def _delete_volume(conn: sqlite3.Connection, volume_id: int) -> None:
    """Delete a volume (and cascade via FK if enabled). Use with caution.

    session_volumes has a FK to volumes WITHOUT ON DELETE CASCADE, so its
    rows must be removed explicitly or the volume delete fails under
    PRAGMA foreign_keys=ON.
    """
    conn.execute("DELETE FROM session_volumes WHERE volume_id = ?", (volume_id,))
    conn.execute("DELETE FROM volume_packs WHERE volume_id = ?", (volume_id,))
    conn.execute("DELETE FROM volumes WHERE volume_id = ?", (volume_id,))
    conn.commit()


def _burn_evidence(conn: sqlite3.Connection, volume_id: int) -> str | None:
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
    - A status note saying the volume was BURNED or VERIFIED when a
      burn of it started (#904,
      :func:`_status_before_burning`).  An LCSAS from
      before #810 (rc1 to rc9, the rc7 install among them) moved a
      BURNED volume to BURNING for its burn, writing the note "Status
      changed: BURNED → BURNING"; a burn killed then leaves the volume
      BURNING, and that note is its only record of the disc.
    - Any other status note out of BURNED or VERIFIED, at any point in
      the volume's history (#912, :func:`_left_burned_or_verified`):
      rc1 to rc9 also demoted a BURNED volume whose burn failed
      (BURNED → BURNING → STAGING), so a later burn from STAGING hides
      the note above, and rc8 and rc9 set a BURNED volume STAGING when
      a failing receipt was imported.  The one note that does not count
      is the one such a failing receipt import wrote: it says only that
      the receipt's disc failed (#483).

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
            if _receipt(detail).get("verify_passed") is not False:
                return f"a burn receipt of it was imported{where}"
        elif event_type == "LOCATION_MOVE":
            return f"a disc of it was moved{where}"
        else:
            return f"a disc of it was burned again{where}"
    before = _status_before_burning(conn, volume_id)
    if before in (STATUS_BURNED, STATUS_VERIFIED):
        return f"it was {before} when a burn of it started"
    return _left_burned_or_verified(conn, volume_id)


def _receipt(detail: str | None) -> dict[str, object]:
    """The receipt provenance a ``BURN_RECEIPT_IMPORTED`` event holds,
    or ``{}`` when its detail is not a JSON object."""
    try:
        receipt = json.loads(detail or "{}")
    except ValueError:
        return {}
    return receipt if isinstance(receipt, dict) else {}


# "Status changed: BURNED → X" or "Status changed: VERIFIED → X", as
# _update_status has written it since before rc1 (d24dcbf).
_LEFT_DURABLE_NOTE = re.compile(
    rf"Status changed: ({STATUS_BURNED}|{STATUS_VERIFIED}) → ([A-Z]+)")


def _left_burned_or_verified(
    conn: sqlite3.Connection, volume_id: int,
) -> str | None:
    """Why a status note says the volume was BURNED or VERIFIED, or None
    (#912).  The newest such note that counts gives the reason.

    Every note out of BURNED or VERIFIED counts except "Status changed:
    BURNED → STAGING" when the volume's next two events are the
    ``VERIFY_FAIL`` and the ``BURN_RECEIPT_IMPORTED`` (``verify_passed``
    false) that the same receipt import wrote.  That is what
    ``catalog import-receipts`` wrote, from c5b3338 (rc8, rc9) until
    #810 (7935657, rc10), for a failing receipt of a BURNED volume with
    no disc recorded, in one transaction under the catalog lock:

    1. ``NOTE`` "Status changed: BURNED → STAGING" (``update_status``);
    2. ``VERIFY_FAIL`` at the receipt's location, detail "Offline burn
       receipt: <file name>";
    3. ``BURN_RECEIPT_IMPORTED`` at that location, its JSON detail with
       ``"receipt_file": "<file name>"`` and ``"verify_passed": false``.

    So the three are adjacent in the volume's events, and the match is
    on that order, the location and the file name.  Not on the dates:
    each ``add_event`` stamped its own ``datetime.now()``.  Not on
    consecutive ``event_id`` values either: ``catalog rebuild`` copies
    events into a catalog with new ids.  No other LCSAS from rc1 to rc9
    wrote "BURNED → STAGING" (its burns went BURNED → BURNING first),
    and none paired a failing receipt with any other note out of BURNED
    or VERIFIED, so the exception covers only this note.  rc2 to rc7
    changed no status for a failing receipt; rc1 recorded its disc and
    wrote no ``verify_passed`` in the event.  Since #810 a failing
    receipt leaves a BURNED volume BURNED.
    """
    events = conn.execute(
        """SELECT event_type, location, detail FROM volume_events
           WHERE volume_id = ?
           ORDER BY julianday(event_date), event_id""",
        (volume_id,),
    ).fetchall()
    reason = None
    for i, (event_type, _location, detail) in enumerate(events):
        match = (_LEFT_DURABLE_NOTE.match(str(detail or ""))
                 if event_type == "NOTE" else None)
        if match is None or _failing_receipt_wrote(events, i):
            continue
        status, new_status = match.groups()
        if new_status == STATUS_BURNING:
            # Not the newest "→ BURNING" note: _burn_evidence reads that
            # one first.
            reason = f"it was {status} when an earlier burn of it started"
        else:
            reason = f"it was {status} before it was set to {new_status}"
    return reason


def _failing_receipt_wrote(
    events: list[tuple[str, str | None, str | None]], i: int,
) -> bool:
    """True when ``events[i]`` is the "BURNED → STAGING" note that a
    failing receipt import of rc8 or rc9 wrote, followed by its
    ``VERIFY_FAIL`` and ``BURN_RECEIPT_IMPORTED``
    (:func:`_left_burned_or_verified`)."""
    if events[i][2] != f"Status changed: {STATUS_BURNED} → {STATUS_STAGING}":
        return False
    if len(events) < i + 3:
        return False
    fail_type, fail_location, fail_detail = events[i + 1]
    import_type, import_location, import_detail = events[i + 2]
    receipt = _receipt(import_detail)
    return (fail_type == "VERIFY_FAIL"
            and import_type == "BURN_RECEIPT_IMPORTED"
            and fail_location == import_location
            and receipt.get("verify_passed") is False
            and fail_detail
            == f"Offline burn receipt: {receipt.get('receipt_file')}")


def _rebuild_burn_unknown(conn: sqlite3.Connection, volume_id: int) -> bool:
    """True when nobody knows whether a disc of the volume was burned.

    A catalog rebuilt from discs knows a volume of a newer burn session
    only from the disc catalogs of discs staged after it, all copied
    before that volume was burned.  Unless its own disc was read
    (``db/rebuild.py`` then marks it BURNED), it comes back STAGING with
    no disc record and no events, which is how a never-burned volume
    looks.  The rebuild writes a ``NOTE`` starting with
    :data:`REBUILD_UNKNOWN_NOTE_PREFIX` on each such volume.

    True while that holds: the volume is STAGING or BURNING, has the
    note, no ``volume_copies`` row and no :func:`_burn_evidence`.
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
    return row is not None and _burn_evidence(conn, volume_id) is None


@dataclass(frozen=True)
class Burn:
    """A burn of one volume in progress: what :meth:`VolumeLifecycle.
    burn_started` decided, which the burn's outcome event needs."""

    volume_id: int
    # VERIFIED, or a lost volume (#726): the burn adds a disc and keeps
    # the status.
    reburn: bool
    # A never-burned volume, moved to BURNING; it goes back to STAGING if
    # the burn fails (#483) or is stopped (#668).
    marked_burning: bool


@dataclass(frozen=True)
class Refusal:
    """Why a volume may not be deleted (:meth:`VolumeLifecycle.may_delete`).

    *kind* is one of ``"status"`` (not STAGING or BURNING), ``"disc"`` (a
    disc of it is recorded), ``"burned"`` (an event says a good disc of it
    was burned; *evidence* says which) and ``"burn unknown"`` (a catalog
    rebuild does not know whether it was burned, #758)."""

    kind: str
    status: str
    evidence: str | None = None


class VolumeLifecycle:
    """The status changes of the volumes in one catalog, by event."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -----------------------------------------------------------------
    # Staging
    # -----------------------------------------------------------------

    def stage_failed(self, volume_id: int) -> None:
        """A stage could not finish the volume it was making (mastering,
        ECC or re-mastering failed): delete it, so its packs go back to the
        unarchived pool.  Nothing is checked: the stage made the volume and
        nothing else has seen it.  Commits."""
        _delete_volume(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Burning
    # -----------------------------------------------------------------

    def burn_started(self, volume_id: int) -> Burn:
        """A burn of the volume starts.

        A never-burned (STAGING) volume becomes BURNING, committed, so a
        burn killed outright is seen by the next one (#668).  A VERIFIED
        or lost volume gets another disc and keeps its status; a BURNED
        one stays BURNED for the whole burn (#810).  Any other status is
        refused (``ValueError``, the reason :meth:`may_burn` gives)."""
        from lcsas.db.queries import deprecated_by_loss

        refusal = self.may_burn(volume_id)
        if refusal is not None:
            raise ValueError(refusal)
        vol = get_volume_by_id(self._conn, volume_id)
        reburn = vol.status == STATUS_VERIFIED or (
            vol.status == STATUS_DEPRECATED
            and deprecated_by_loss(self._conn, volume_id)
        )
        marked_burning = not reburn and vol.status != STATUS_BURNED
        if marked_burning:
            _update_status(self._conn, volume_id, STATUS_BURNING, commit=False)
            self._conn.commit()
        return Burn(volume_id, reburn, marked_burning)

    def burn_succeeded(
        self,
        burn: Burn,
        *,
        location: str,
        burn_date: str,
        iso_sha256: str | None,
        iso_size_bytes: int | None,
        verified_at: str | None,
    ) -> Volume:
        """The disc was burned and read back: the volume becomes VERIFIED
        and closed (unless it was a re-burn), and the disc is recorded at
        *location* (which puts a lost volume back in service, #726)."""
        from lcsas.db.volume_copies import add_volume_copy

        if not burn.reburn:
            _update_status(self._conn, burn.volume_id, STATUS_VERIFIED,
                          commit=False)
            mark_closed(self._conn, burn.volume_id, commit=False)
        add_volume_copy(
            self._conn,
            volume_id=burn.volume_id,
            location=location,
            burn_date=burn_date,
            iso_sha256=iso_sha256,
            iso_size_bytes=iso_size_bytes,
            last_verified_at=verified_at,
            verified=True,
            commit=False,
        )
        return get_volume_by_id(self._conn, burn.volume_id)

    def burn_failed(
        self, burn: Burn, *, location: str, burn_date: str,
    ) -> Volume:
        """The burned disc failed its read-back.  No disc is recorded
        (BURN-05).  A never-burned volume goes back to STAGING (#483); a
        re-burned one keeps its status and gets a ``VERIFY_FAIL_REBURN``
        event; a BURNED one stays BURNED (#810)."""
        if burn.marked_burning:
            _update_status(self._conn, burn.volume_id, STATUS_STAGING,
                          commit=False)
        elif burn.reburn:
            add_event(
                self._conn, burn.volume_id, "VERIFY_FAIL_REBURN",
                location=location,
                detail="Post-burn read-back failed on re-burn attempt",
                event_date=burn_date,
                commit=False,
            )
        return get_volume_by_id(self._conn, burn.volume_id)

    def burn_stopped(self, burn: Burn) -> Volume | None:
        """The burn was stopped by an error or Ctrl-C (the caller has
        rolled back what it had not committed): a volume the burn moved to
        BURNING goes back to STAGING, committed (#668).  Returns the volume
        when its status changed; else None, and nothing is read (the
        catalog may be what failed)."""
        if not burn.marked_burning:
            return None
        _update_status(self._conn, burn.volume_id, STATUS_STAGING)
        return get_volume_by_id(self._conn, burn.volume_id)

    def burn_interrupted_recovered(self, volume_id: int) -> Volume:
        """A BURNING volume with no disc recorded, left by a burn killed
        outright, goes back to the status it had before that burn:
        STAGING, or BURNED when an older LCSAS moved a BURNED volume to
        BURNING (#668, #810).  Commits."""
        _update_status(self._conn, volume_id,
                      _status_before_burning(self._conn, volume_id))
        return get_volume_by_id(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Verifying
    # -----------------------------------------------------------------

    def verified_by_hand(
        self,
        volume_id: int,
        *,
        location: str | None,
        record_disc: bool,
        detail: str,
    ) -> Volume:
        """The owner says a disc of the volume passed a check done
        elsewhere (``verify --mark-verified``): STAGING or BURNED becomes
        VERIFIED.  With *record_disc* the disc is recorded at *location*
        with the staged ISO's hash (``last_verified_at`` stays NULL:
        nothing here read it).  The volume is closed and a
        ``VERIFY_PASS`` event written.  Commits."""
        from lcsas.db.locations import ensure_location
        from lcsas.db.sessions import get_staged_iso
        from lcsas.db.volume_copies import add_volume_copy

        vol = get_volume_by_id(self._conn, volume_id)
        if vol.status == STATUS_STAGING:
            _update_status(self._conn, volume_id, STATUS_BURNING, commit=False)
        _update_status(self._conn, volume_id, STATUS_VERIFIED, commit=False)
        if record_disc and location:
            ensure_location(self._conn, location, commit=False)
            staged = get_staged_iso(self._conn, volume_id)
            add_volume_copy(
                self._conn, volume_id, location,
                notes="Recorded by verify --mark-verified",
                iso_sha256=staged[0] if staged else None,
                iso_size_bytes=staged[1] if staged else None,
                commit=False,
            )
        if vol.closed_at is None:
            mark_closed(self._conn, volume_id, commit=False)
        add_event(
            self._conn, volume_id, "VERIFY_PASS",
            location=location,
            detail=detail,
        )
        return get_volume_by_id(self._conn, volume_id)

    # A check of the staged ISO image (``lcsas verify <label>``, ``verify
    # --all``) is no event: VERIFIED means a disc of the volume was read
    # back (#906, CONTEXT.md "Verified volume").

    def disc_check_passed(
        self, volume_id: int, *, disc_recorded: bool = False,
    ) -> Volume | None:
        """A disc of the volume was identified and read back
        (``verify --disc``, which needs the disc recorded or ``--location``,
        #906): a BURNED volume becomes VERIFIED.  With
        *disc_recorded* (the check recorded that disc, #678), so does a
        STAGING or BURNING one: the same evidence a burn's own check
        gives.  Commits.  Returns the volume when its status changed,
        else None."""
        status = get_volume_by_id(self._conn, volume_id).status
        if status == STATUS_BURNED:
            _update_status(self._conn, volume_id, STATUS_VERIFIED)
        elif disc_recorded and status in (STATUS_STAGING, STATUS_BURNING):
            _update_status(self._conn, volume_id, STATUS_VERIFIED, force=True)
        else:
            return None
        return get_volume_by_id(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Burn receipts
    # -----------------------------------------------------------------

    def passing_receipt_imported(
        self,
        volume_id: int,
        *,
        first_disc: bool,
        location: str,
        burn_date: str,
        iso_sha256: str | None,
        iso_size_bytes: int | None,
        verified_at: str | None,
        detail: str,
    ) -> Volume:
        """A receipt says a disc of the volume was burned and read back at
        *location*.  Its first disc makes the volume VERIFIED and closed.
        A ``VERIFY_PASS`` event is written and the disc recorded."""
        from lcsas.db.volume_copies import add_volume_copy

        if first_disc:
            if get_volume_by_id(self._conn, volume_id).status == STATUS_STAGING:
                _update_status(self._conn, volume_id, STATUS_BURNING,
                              commit=False)
            _update_status(self._conn, volume_id, STATUS_VERIFIED,
                          commit=False)
            mark_closed(self._conn, volume_id, commit=False)
        # One VERIFY_PASS per passing disc, as the direct burn writes:
        # `lcsas status` reads the newest verify result at each location
        # (#671).
        add_event(self._conn, volume_id, "VERIFY_PASS",
                  location=location, detail=detail, commit=False)
        add_volume_copy(
            self._conn,
            volume_id=volume_id,
            location=location,
            burn_date=burn_date,
            iso_sha256=iso_sha256,
            iso_size_bytes=iso_size_bytes,
            last_verified_at=verified_at,
            commit=False,
        )
        return get_volume_by_id(self._conn, volume_id)

    def unverified_receipt_imported(
        self,
        volume_id: int,
        *,
        location: str,
        burn_date: str,
        iso_sha256: str | None,
        iso_size_bytes: int | None,
    ) -> Volume:
        """A receipt says a disc was burned at *location* without a
        read-back (``burn-iso --no-verify``): a STAGING or BURNING volume
        becomes BURNED, not VERIFIED, and the disc is recorded with no
        ``last_verified_at``."""
        from lcsas.db.volume_copies import add_volume_copy

        status = get_volume_by_id(self._conn, volume_id).status
        if status == STATUS_STAGING:
            _update_status(self._conn, volume_id, STATUS_BURNING, commit=False)
        if status in (STATUS_STAGING, STATUS_BURNING):
            _update_status(self._conn, volume_id, STATUS_BURNED, commit=False)
        add_volume_copy(
            self._conn,
            volume_id=volume_id,
            location=location,
            burn_date=burn_date,
            iso_sha256=iso_sha256,
            iso_size_bytes=iso_size_bytes,
            last_verified_at=None,
            commit=False,
        )
        return get_volume_by_id(self._conn, volume_id)

    def failing_receipt_imported(
        self,
        volume_id: int,
        *,
        first_disc: bool,
        location: str,
        detail: str,
    ) -> Volume:
        """A receipt says the disc burned at *location* failed its
        read-back.  No disc is recorded.  For the volume's first disc
        (none recorded), a BURNING volume goes back to the status it had
        before its burn and a ``VERIFY_FAIL`` event is written (#483); a
        BURNED one stays BURNED (#810).  Otherwise the event is
        ``VERIFY_FAIL_REBURN``."""
        from lcsas.db.volume_copies import get_copies_for_volume

        if first_disc and not get_copies_for_volume(self._conn, volume_id):
            if get_volume_by_id(self._conn, volume_id).status == STATUS_BURNING:
                _update_status(
                    self._conn, volume_id,
                    _status_before_burning(self._conn, volume_id),
                    commit=False,
                )
            add_event(self._conn, volume_id, "VERIFY_FAIL",
                      location=location, detail=detail, commit=False)
        else:
            add_event(self._conn, volume_id, "VERIFY_FAIL_REBURN",
                      location=location, detail=detail, commit=False)
        return get_volume_by_id(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Discs
    # -----------------------------------------------------------------

    def last_disc_lost(
        self, volume_id: int, *, location: str, action: str,
    ) -> Volume | None:
        """A disc of the volume went out of service (*action*: deprecated,
        destroyed or lost) at *location*.  When it was the last in-service
        disc, the volume is a lost volume.  Returns it when it was, else
        None.

        There is no LOST status in the schema (Q33, ADR 0003): a lost
        volume is DEPRECATED plus the ``auto-deprecated:`` NOTE written
        here, and owner displays show it as LOST
        (``lcsas.db.queries.get_lost_volume_ids``).

        Replica truth (BURN-10): a volume is a live replica iff it has >=1
        ACTIVE disc — except a volume with zero ``volume_copies`` rows at
        all, which counts by status alone (legacy).  A volume whose disc
        rows exist but none are ACTIVE must therefore lose its durable
        status, or the redundancy report and deprecation guard keep
        treating a destroyed disc as a valid replica.

        Outside the status graph: DEPRECATED is not a legal transition
        from every durable state (e.g. BURNED), and the deprecation-safety
        guard must not veto recording physical reality.  Already
        DEPRECATED or DESTROYED volumes are left alone (never resurrect a
        DESTROYED volume).
        """
        row = self._conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END), 0)
                          AS active
               FROM volume_copies WHERE volume_id = ?""",
            (volume_id,),
        ).fetchone()
        if row["total"] == 0 or row["active"] > 0:
            return None
        result = self._conn.execute(
            """UPDATE volumes SET status = 'DEPRECATED'
               WHERE volume_id = ? AND status NOT IN ('DEPRECATED', 'DESTROYED')""",
            (volume_id,),
        )
        if result.rowcount == 0:
            return None
        # The prefix is what lcsas.db.queries.get_lost_volume_ids matches
        # to tell a lost volume from one deprecated by consolidation (ADR
        # 0003).
        add_event(
            self._conn, volume_id, "NOTE",
            location=location,
            detail=(
                f"{AUTO_DEPRECATED_NOTE_PREFIX} last in-service disc {action} "
                f"at {location}"
            ),
            commit=False,
        )
        return get_volume_by_id(self._conn, volume_id)

    def disc_back_in_service(
        self, volume_id: int, *, location: str, verified: bool,
    ) -> Volume | None:
        """A disc of the volume is in service at *location* (recorded, or
        read again).  A lost volume (:meth:`last_disc_lost`) is back in
        service: VERIFIED when the disc was verified, else BURNED, as a
        first disc would (#726, #752).  Then the stage pool, the redundancy
        report and the deprecation guard count it again.  A volume
        deprecated by consolidation is left alone
        (``lcsas.db.queries.deprecated_by_loss``).  Returns the volume when
        its status changed, else None.

        Outside the status graph: DEPRECATED → VERIFIED is not a general
        transition, only this one.  The NOTE says why.
        """
        from lcsas.db.queries import deprecated_by_loss

        if not deprecated_by_loss(self._conn, volume_id):
            return None
        status = STATUS_VERIFIED if verified else STATUS_BURNED
        if verified:
            self._conn.execute(
                "UPDATE volumes SET status = ?, verified_at = ? WHERE volume_id = ?",
                (status, utc_now(), volume_id),
            )
        else:
            self._conn.execute(
                "UPDATE volumes SET status = ? WHERE volume_id = ?",
                (status, volume_id),
            )
        add_event(
            self._conn, volume_id, "NOTE",
            location=location,
            detail=(
                f"Status changed: DEPRECATED → {status}; this lost volume has "
                f"a disc in service again at {location}"
            ),
            commit=False,
        )
        vol = get_volume_by_id(self._conn, volume_id)
        _logger.info(
            "%s was a lost volume; with its disc at %s it is in service "
            "again (%s).", vol.label, location, status,
        )
        return vol

    # -----------------------------------------------------------------
    # Consolidation
    # -----------------------------------------------------------------

    def consolidation_started(
        self, volume_id: int, *, note: str | None = None,
    ) -> Volume:
        """The volume's packs are being consolidated onto a new volume: it
        becomes CONSOLIDATING.  *note*, when given, is written as a NOTE
        event after the status change (the burn session it ties the
        volume to, #677)."""
        _update_status(self._conn, volume_id, STATUS_CONSOLIDATING,
                      commit=False)
        if note is not None:
            add_event(self._conn, volume_id, "NOTE", detail=note,
                      commit=False)
        return get_volume_by_id(self._conn, volume_id)

    def consolidation_finished(self, volume_id: int) -> Volume:
        """The volume's packs are on another volume: a VERIFIED or
        CONSOLIDATING volume becomes DEPRECATED.  Refused (``ValueError``,
        a one-line reason for the owner, nothing written) when the status
        graph forbids it, or when a pack would be on no other live volume.
        A never-burned volume is deleted or staged again, never deprecated
        (#905)."""
        vol = get_volume_by_id(self._conn, volume_id)
        if STATUS_DEPRECATED not in _TRANSITIONS.get(vol.status, set()):
            why = {
                STATUS_STAGING: (
                    f"it was never burned, so it is not deprecated; delete "
                    f"it with 'lcsas session abort --volume {vol.label}' "
                    f"(its packs go back to the unarchived pool) or burn it"
                ),
                STATUS_BURNED: (
                    f"verify its disc first ('lcsas verify {vol.label} "
                    f"--disc')"
                ),
                STATUS_DEPRECATED: "it is already deprecated",
            }.get(vol.status, "only a VERIFIED or CONSOLIDATING volume is "
                              "deprecated")
            raise ValueError(
                f"Cannot deprecate volume {vol.label}: it is {vol.status}; "
                f"{why}."
            )
        _update_status(self._conn, volume_id, STATUS_DEPRECATED, commit=False)
        return get_volume_by_id(self._conn, volume_id)

    def consolidation_abandoned(self, volume_id: int) -> Volume:
        """The consolidation of the volume will not finish (its session was
        aborted): it goes back to VERIFIED."""
        _update_status(self._conn, volume_id, STATUS_VERIFIED, commit=False)
        return get_volume_by_id(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Deleting
    # -----------------------------------------------------------------

    def may_delete(
        self, volume_id: int, *, never_burned: bool = False,
    ) -> Refusal | None:
        """Whether a never-burned volume may be deleted, so its packs go
        back to the unarchived pool (``session abort``, ``stage --clean
        --force``, ``catalog reconcile --fix``).

        Only a STAGING or BURNING volume with no disc recorded and no burn
        evidence may; one a catalog rebuild left with no record of whether
        it was burned (#758) only with *never_burned* (the owner confirms
        it never was).  Returns why not, or None."""
        vol = get_volume_by_id(self._conn, volume_id)
        if vol.status not in (STATUS_STAGING, STATUS_BURNING):
            return Refusal("status", vol.status)
        if self._conn.execute(
            "SELECT 1 FROM volume_copies WHERE volume_id = ? LIMIT 1",
            (volume_id,),
        ).fetchone() is not None:
            return Refusal("disc", vol.status)
        evidence = _burn_evidence(self._conn, volume_id)
        if evidence is not None:
            return Refusal("burned", vol.status, evidence)
        if not never_burned and _rebuild_burn_unknown(self._conn, volume_id):
            return Refusal("burn unknown", vol.status)
        return None

    def never_burned_deleted(
        self, volume_id: int, *, never_burned: bool = False,
    ) -> None:
        """Delete a never-burned volume (:meth:`may_delete`); its packs go
        back to the unarchived pool.  Raises ``ValueError`` when it may not
        be deleted.  Commits."""
        refusal = self.may_delete(volume_id, never_burned=never_burned)
        if refusal is not None:
            raise ValueError(
                f"Volume {volume_id} may not be deleted ({refusal.kind}: "
                f"{refusal.status})"
            )
        _delete_volume(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Catalog rebuild
    # -----------------------------------------------------------------

    def disc_catalog_status_adopted(self, volume_id: int, status: str) -> None:
        """A catalog rebuild takes the volume's status from the freshest
        disc catalog that lists it, downgrades included.  No note: the
        rebuilt catalog's newest timestamp is its freshness."""
        self._conn.execute(
            "UPDATE volumes SET status = ? WHERE volume_id = ?",
            (status, volume_id),
        )

    def own_disc_read_in_rebuild(self, volume_id: int) -> None:
        """A catalog rebuild read the volume's own disc: a STAGING or
        BURNING volume becomes BURNED (#678; its disc is not recorded,
        and ``verify --disc --location`` records it).  No note, as
        :meth:`disc_catalog_status_adopted`."""
        self._conn.execute(
            "UPDATE volumes SET status = 'BURNED' WHERE volume_id = ?",
            (volume_id,),
        )

    # -----------------------------------------------------------------
    # Read-only
    # -----------------------------------------------------------------

    def may_burn(self, volume_id: int) -> str | None:
        """Whether a new disc of the volume may be burned: why not, as one
        line for the owner, or None.

        A STAGING, BURNED or VERIFIED volume, or a lost one (#726), may.
        A CONSOLIDATING, DEPRECATED or DESTROYED one may not (#905): its
        packs are being moved, or are on other volumes, or it is out of
        service.  ``lcsas burn`` asks before its first disc prompt, and
        :meth:`burn_started` refuses with the same reason."""
        from lcsas.db.queries import deprecated_by_loss

        vol = get_volume_by_id(self._conn, volume_id)
        if vol.status in (STATUS_STAGING, STATUS_BURNED, STATUS_VERIFIED):
            return None
        if (vol.status == STATUS_DEPRECATED
                and deprecated_by_loss(self._conn, volume_id)):
            return None
        why = {
            STATUS_BURNING: (
                "a burn of it was interrupted; 'lcsas burn' of its session "
                "resets it"
            ),
            STATUS_CONSOLIDATING: (
                "its packs are being consolidated onto a new volume; burn "
                "that session and run 'lcsas consolidate --deprecate', or "
                "'lcsas session abort' it to make this volume VERIFIED again"
            ),
            STATUS_DEPRECATED: (
                "its packs are on other volumes, so no new disc of it is "
                "burned"
            ),
        }.get(vol.status, "it is out of service, so no new disc of it is "
                          "burned")
        return f"Cannot burn volume {vol.label}: it is {vol.status}; {why}."

    def burn_evidence(self, volume_id: int) -> str | None:
        """Why the volume's events say a good disc of it was burned, or
        None (the check before deleting a volume with no disc record)."""
        return _burn_evidence(self._conn, volume_id)

    def burn_unknown(self, volume_id: int) -> bool:
        """True when a catalog rebuild left the volume with no record of
        whether a disc of it was burned (#758)."""
        return _rebuild_burn_unknown(self._conn, volume_id)
