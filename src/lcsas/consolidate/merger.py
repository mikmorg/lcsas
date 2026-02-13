"""Volume consolidation — merge multiple small volumes into a larger one."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lcsas.config.media import MediaType
from lcsas.db.models import Pack, Volume
from lcsas.db.queries import get_packs_only_on_volumes
from lcsas.db.volumes import get_volume_by_id, list_volumes, update_status


@dataclass
class ConsolidationPlan:
    """Plan for merging source volumes into a target volume."""

    source_volume_ids: list[int]
    source_labels: list[str]
    active_packs: list[Pack]
    total_active_bytes: int
    target_media_type: MediaType
    volumes_needed: int


class VolumeMerger:
    """Plans and executes volume consolidation.

    Consolidation reads active (non-pruned) packs from the local mirror
    (no disc insertion needed) and burns them to new, larger volumes.
    Source volumes are then marked as DEPRECATED.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

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

        # Estimate volumes needed
        from lcsas.binpack.algorithm import estimate_volumes_needed
        volumes_needed = estimate_volumes_needed(
            total_bytes,
            target_media_type.capacity_bytes,
            reserved=104_857_600,  # 100 MB metadata
            ecc_overhead_pct=target_media_type.ecc_overhead_pct,
        )

        return ConsolidationPlan(
            source_volume_ids=source_volume_ids,
            source_labels=source_labels,
            active_packs=active_packs,
            total_active_bytes=total_bytes,
            target_media_type=target_media_type,
            volumes_needed=volumes_needed,
        )

    def deprecate_sources(
        self,
        source_volume_ids: list[int],
    ) -> None:
        """Mark source volumes as DEPRECATED after successful consolidation."""
        for vid in source_volume_ids:
            update_status(self._conn, vid, "DEPRECATED")
