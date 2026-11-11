"""Volume consolidation — merge multiple small volumes into a larger one."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lcsas.config.media import MediaType
from lcsas.db.models import Pack
from lcsas.db.queries import get_packs_only_on_volumes, get_pruned_packs_on_volumes
from lcsas.db.volumes import get_volume_by_id, update_status


def consolidation_note(session_id: str) -> str:
    """Detail of the NOTE event that ties a source volume to the burn
    session its packs were staged into (#677).  ``session abort`` of that
    session finds the sources by it (no schema change: volume_events)."""
    return f"consolidation: packs staged into burn session {session_id}"


def release_consolidation_sources(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[str]:
    """Return the CONSOLIDATING sources of *session_id* to VERIFIED.

    Called when that burn session is aborted, so its sources do not stay
    CONSOLIDATING with nothing left to finish.  Returns their labels.
    """
    rows = conn.execute(
        """SELECT DISTINCT v.volume_id, v.label FROM volumes v
           JOIN volume_events e ON e.volume_id = v.volume_id
           WHERE v.status = 'CONSOLIDATING'
             AND e.event_type = 'NOTE'
             AND e.detail = ?
           ORDER BY v.label""",
        (consolidation_note(session_id),),
    ).fetchall()
    for row in rows:
        update_status(conn, row[0], "VERIFIED", commit=False)
    conn.commit()
    return [row[1] for row in rows]


@dataclass
class ConsolidationPlan:
    """Plan for merging source volumes into a target volume."""

    source_volume_ids: list[int]
    source_labels: list[str]
    active_packs: list[Pack]
    total_active_bytes: int
    target_media_type: MediaType
    volumes_needed: int
    # Pruned packs on the source volumes that will NOT migrate — surfaced
    # so a mis-pruned pack is caught before its discs are deprecated (BURN-09).
    pruned_left_behind: list[Pack] = field(default_factory=list)


class VolumeMerger:
    """Plans and executes volume consolidation.

    Consolidation reads active (non-pruned) packs from the local mirror
    (no disc insertion needed) and burns them to new, larger volumes.
    Source volumes are then marked as DEPRECATED.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        metadata_reserve_bytes: int = 104_857_600,
    ) -> None:
        self._conn = conn
        self._metadata_reserve_bytes = metadata_reserve_bytes

    def plan_consolidation(
        self,
        source_volume_ids: list[int],
        target_media_type: MediaType,
    ) -> ConsolidationPlan:
        """Create a consolidation plan.

        Args:
            source_volume_ids: IDs of volumes to merge.
            target_media_type: Media type for the target volume(s).

        Returns:
            ConsolidationPlan with the list of active packs to migrate.
        """
        # Validate source volumes exist
        source_labels: list[str] = []
        for vid in source_volume_ids:
            vol = get_volume_by_id(self._conn, vid)
            source_labels.append(vol.label)

        # Get active packs from source volumes
        active_packs = get_packs_only_on_volumes(self._conn, source_volume_ids)
        total_bytes = sum(p.size_bytes for p in active_packs)

        # Pruned packs stay behind — list them rather than silently exclude.
        pruned_left_behind = get_pruned_packs_on_volumes(
            self._conn, source_volume_ids
        )

        # Estimate volumes needed
        from lcsas.binpack.algorithm import estimate_volumes_needed
        volumes_needed = estimate_volumes_needed(
            total_bytes,
            target_media_type.capacity_bytes,
            reserved=self._metadata_reserve_bytes,
            ecc_overhead_pct=target_media_type.ecc_overhead_pct,
        )

        return ConsolidationPlan(
            source_volume_ids=source_volume_ids,
            source_labels=source_labels,
            active_packs=active_packs,
            total_active_bytes=total_bytes,
            target_media_type=target_media_type,
            volumes_needed=volumes_needed,
            pruned_left_behind=pruned_left_behind,
        )

    def deprecate_sources(
        self,
        source_volume_ids: list[int],
    ) -> None:
        """Mark source volumes as DEPRECATED after successful consolidation.

        This is the second step of a two-phase consolidation.  Call
        :meth:`mark_sources_consolidating` *before* starting the burn so
        that a crash leaves volumes in a recognisable interrupted state rather
        than silently ACTIVE.
        """
        for vid in source_volume_ids:
            update_status(self._conn, vid, "DEPRECATED", commit=False)
        self._conn.commit()

    def mark_sources_consolidating(
        self,
        source_volume_ids: list[int],
        session_id: str | None = None,
    ) -> None:
        """Transition source volumes to CONSOLIDATING before the burn starts.

        Marking volumes CONSOLIDATING before the burn provides a crash
        marker: if the process crashes, the owner can inspect the catalog
        and either re-run the burn (transition back to VERIFIED) or
        complete the deprecation manually, rather than finding volumes
        silently stuck as ACTIVE with no indication of intent.

        With *session_id* (the burn session the packs were staged into),
        each source also gets a :func:`consolidation_note` event, so
        aborting that session returns the sources to VERIFIED (#677).
        """
        from lcsas.db.volume_events import add_event

        for vid in source_volume_ids:
            update_status(self._conn, vid, "CONSOLIDATING", commit=False)
            if session_id is not None:
                add_event(
                    self._conn, vid, "NOTE",
                    detail=consolidation_note(session_id), commit=False,
                )
        self._conn.commit()

    def abort_consolidation(
        self,
        source_volume_ids: list[int],
    ) -> None:
        """Revert CONSOLIDATING volumes back to VERIFIED on burn failure."""
        for vid in source_volume_ids:
            update_status(self._conn, vid, "VERIFIED", commit=False)
        self._conn.commit()
