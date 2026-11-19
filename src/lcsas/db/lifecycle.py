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

import sqlite3
from dataclasses import dataclass

from lcsas.constants import (
    STATUS_BURNED,
    STATUS_BURNING,
    STATUS_CONSOLIDATING,
    STATUS_DEPRECATED,
    STATUS_STAGING,
    STATUS_VERIFIED,
)
from lcsas.db.models import Volume
from lcsas.db.volume_events import (
    add_event,
    burn_evidence,
    rebuild_burn_unknown,
)
from lcsas.db.volumes import (
    delete_volume,
    get_volume_by_id,
    mark_closed,
    status_before_burning,
    update_status,
)


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
        delete_volume(self._conn, volume_id)

    # -----------------------------------------------------------------
    # Burning
    # -----------------------------------------------------------------

    def burn_started(self, volume_id: int) -> Burn:
        """A burn of the volume starts.

        A never-burned (STAGING) volume becomes BURNING, committed, so a
        burn killed outright is seen by the next one (#668).  A VERIFIED
        or lost volume gets another disc and keeps its status; a BURNED
        one stays BURNED for the whole burn (#810).  Any other status is
        refused (``ValueError``, from the status graph)."""
        from lcsas.db.queries import deprecated_by_loss

        vol = get_volume_by_id(self._conn, volume_id)
        reburn = vol.status == STATUS_VERIFIED or (
            vol.status == STATUS_DEPRECATED
            and deprecated_by_loss(self._conn, volume_id)
        )
        marked_burning = not reburn and vol.status != STATUS_BURNED
        if marked_burning:
            update_status(self._conn, volume_id, STATUS_BURNING, commit=False)
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
            update_status(self._conn, burn.volume_id, STATUS_VERIFIED,
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
            update_status(self._conn, burn.volume_id, STATUS_STAGING,
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
        update_status(self._conn, burn.volume_id, STATUS_STAGING)
        return get_volume_by_id(self._conn, burn.volume_id)

    def burn_interrupted_recovered(self, volume_id: int) -> Volume:
        """A BURNING volume with no disc recorded, left by a burn killed
        outright, goes back to the status it had before that burn:
        STAGING, or BURNED when an older LCSAS moved a BURNED volume to
        BURNING (#668, #810).  Commits."""
        update_status(self._conn, volume_id,
                      status_before_burning(self._conn, volume_id))
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
            update_status(self._conn, volume_id, STATUS_BURNING, commit=False)
        update_status(self._conn, volume_id, STATUS_VERIFIED, commit=False)
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

    def iso_check_passed(self, volume_id: int) -> Volume | None:
        """The volume's staged ISO passed a check (``lcsas verify <label>``,
        ``verify --all``): a BURNED volume becomes VERIFIED, committed.
        Returns the volume when its status changed, else None."""
        if get_volume_by_id(self._conn, volume_id).status != STATUS_BURNED:
            return None
        update_status(self._conn, volume_id, STATUS_VERIFIED)
        return get_volume_by_id(self._conn, volume_id)

    def disc_check_passed(
        self, volume_id: int, *, disc_recorded: bool = False,
    ) -> Volume | None:
        """A disc of the volume was identified and read back
        (``verify --disc``): a BURNED volume becomes VERIFIED.  With
        *disc_recorded* (the check recorded that disc, #678), so does a
        STAGING or BURNING one: the same evidence a burn's own check
        gives.  Commits.  Returns the volume when its status changed,
        else None."""
        status = get_volume_by_id(self._conn, volume_id).status
        if status == STATUS_BURNED:
            update_status(self._conn, volume_id, STATUS_VERIFIED)
        elif disc_recorded and status in (STATUS_STAGING, STATUS_BURNING):
            update_status(self._conn, volume_id, STATUS_VERIFIED, force=True)
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
                update_status(self._conn, volume_id, STATUS_BURNING,
                              commit=False)
            update_status(self._conn, volume_id, STATUS_VERIFIED,
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
            update_status(self._conn, volume_id, STATUS_BURNING, commit=False)
        if status in (STATUS_STAGING, STATUS_BURNING):
            update_status(self._conn, volume_id, STATUS_BURNED, commit=False)
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
                update_status(
                    self._conn, volume_id,
                    status_before_burning(self._conn, volume_id),
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
    ) -> bool:
        """A disc of the volume went out of service (*action*: deprecated,
        destroyed or lost) at *location*.  When it was the last in-service
        disc, the volume is recorded as lost: DEPRECATED plus an
        ``auto-deprecated:`` note (ADR 0003).  Returns True when it was."""
        from lcsas.db.volume_copies import _demote_volume_if_last_copy_lost

        return _demote_volume_if_last_copy_lost(
            self._conn, volume_id, location, action)

    def disc_back_in_service(
        self, volume_id: int, *, location: str, verified: bool,
    ) -> str | None:
        """A disc of the volume is in service at *location* (recorded, or
        read again).  A lost volume is back in service: VERIFIED when the
        disc was verified, else BURNED (#726, #752).  A volume deprecated
        by consolidation stays DEPRECATED.  Returns the new status, or
        None when nothing changed."""
        from lcsas.db.volume_copies import _revive_lost_volume

        return _revive_lost_volume(self._conn, volume_id, location,
                                   verified=verified)

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
        update_status(self._conn, volume_id, STATUS_CONSOLIDATING,
                      commit=False)
        if note is not None:
            add_event(self._conn, volume_id, "NOTE", detail=note,
                      commit=False)
        return get_volume_by_id(self._conn, volume_id)

    def consolidation_finished(self, volume_id: int) -> Volume:
        """The volume's packs are on another volume: it becomes
        DEPRECATED.  Refused (``ValueError``) when a pack would be on no
        other live volume, or the status graph forbids it."""
        update_status(self._conn, volume_id, STATUS_DEPRECATED, commit=False)
        return get_volume_by_id(self._conn, volume_id)

    def consolidation_abandoned(self, volume_id: int) -> Volume:
        """The consolidation of the volume will not finish (its session was
        aborted): it goes back to VERIFIED."""
        update_status(self._conn, volume_id, STATUS_VERIFIED, commit=False)
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
        evidence = burn_evidence(self._conn, volume_id)
        if evidence is not None:
            return Refusal("burned", vol.status, evidence)
        if not never_burned and rebuild_burn_unknown(self._conn, volume_id):
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
        delete_volume(self._conn, volume_id)

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

    def burn_evidence(self, volume_id: int) -> str | None:
        """Why the volume's events say a good disc of it was burned, or
        None (the check before deleting a volume with no disc record)."""
        return burn_evidence(self._conn, volume_id)

    def burn_unknown(self, volume_id: int) -> bool:
        """True when a catalog rebuild left the volume with no record of
        whether a disc of it was burned (#758)."""
        return rebuild_burn_unknown(self._conn, volume_id)
