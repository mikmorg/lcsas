"""Tests for volume consolidation planner."""

from __future__ import annotations

from lcsas.config.media import MediaType
from lcsas.consolidate.merger import VolumeMerger
from lcsas.db.packs import mark_pruned, register_pack
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, get_volume_by_id
from lcsas.utils.labels import generate_uuid


class TestVolumeMerger:
    def test_plan_consolidation(self, memory_db):
        # Create 3 small volumes with non-overlapping packs
        vols = []
        all_packs = []
        for i in range(3):
            vol = create_volume(
                memory_db, label=f"SMALL_{i}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
                status="VERIFIED",
            )
            vols.append(vol)
            packs = []
            for j in range(5):
                p = register_pack(
                    memory_db, sha256=f"cons_{i}_{j}_hash",
                    size_bytes=10_000, repo_id="_test",
                )
                packs.append(p)
                all_packs.append(p)
            bulk_link_packs(memory_db, vol.volume_id, [p.pack_id for p in packs])

        merger = VolumeMerger(memory_db)
        plan = merger.plan_consolidation(
            [v.volume_id for v in vols],
            MediaType.MDISC100,
        )

        assert len(plan.active_packs) == 15
        assert plan.total_active_bytes == 150_000
        assert plan.volumes_needed >= 1
        assert plan.source_labels == ["SMALL_0", "SMALL_1", "SMALL_2"]

    def test_pruned_packs_excluded(self, memory_db):
        vol = create_volume(
            memory_db, label="PRUNE_SRC", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        p_active = register_pack(memory_db, sha256="active_pack", size_bytes=100, repo_id="_test")
        p_dead = register_pack(memory_db, sha256="dead_pack", size_bytes=200, repo_id="_test")
        mark_pruned(memory_db, p_dead.pack_id)

        bulk_link_packs(memory_db, vol.volume_id, [p_active.pack_id, p_dead.pack_id])

        merger = VolumeMerger(memory_db)
        plan = merger.plan_consolidation([vol.volume_id], MediaType.MDISC100)

        assert len(plan.active_packs) == 1
        assert plan.active_packs[0].sha256 == "active_pack"

    def test_deprecate_sources(self, memory_db):
        vol = create_volume(
            memory_db, label="DEP_ME", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )

        merger = VolumeMerger(memory_db)
        merger.deprecate_sources([vol.volume_id])

        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.status == "DEPRECATED"

    def test_mark_sources_consolidating(self, memory_db):
        """mark_sources_consolidating transitions VERIFIED → CONSOLIDATING."""
        vol = create_volume(
            memory_db, label="CONS_SRC", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        merger = VolumeMerger(memory_db)
        merger.mark_sources_consolidating([vol.volume_id])

        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.status == "CONSOLIDATING"

    def test_abort_consolidation(self, memory_db):
        """abort_consolidation reverts CONSOLIDATING → VERIFIED (crash recovery)."""
        vol = create_volume(
            memory_db, label="CONS_ABORT", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        merger = VolumeMerger(memory_db)
        merger.mark_sources_consolidating([vol.volume_id])
        merger.abort_consolidation([vol.volume_id])

        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.status == "VERIFIED"

    def test_mark_consolidating_then_deprecate(self, memory_db):
        """Full happy path: VERIFIED → CONSOLIDATING → DEPRECATED."""
        vol = create_volume(
            memory_db, label="FULL_CONS", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        # Stage a pack onto another VERIFIED volume so deprecation is safe
        vol2 = create_volume(
            memory_db, label="SAFE_COPY", uuid=generate_uuid(),
            media_type="MDISC100", capacity_bytes=100_000_000_000,
            status="VERIFIED",
        )
        p = register_pack(memory_db, sha256="full_cons_pack", size_bytes=100, repo_id="_test")
        bulk_link_packs(memory_db, vol.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, vol2.volume_id, [p.pack_id])

        merger = VolumeMerger(memory_db)
        merger.mark_sources_consolidating([vol.volume_id])
        merger.deprecate_sources([vol.volume_id])

        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.status == "DEPRECATED"

    def test_mark_consolidating_multiple_volumes(self, memory_db):
        """mark_sources_consolidating handles multiple volumes at once."""
        vols = []
        for i in range(3):
            v = create_volume(
                memory_db, label=f"MULTI_CONS_{i}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
                status="VERIFIED",
            )
            vols.append(v)

        merger = VolumeMerger(memory_db)
        merger.mark_sources_consolidating([v.volume_id for v in vols])

        for v in vols:
            updated = get_volume_by_id(memory_db, v.volume_id)
            assert updated.status == "CONSOLIDATING"

    def test_abort_consolidation_multiple_volumes(self, memory_db):
        """abort_consolidation reverts all CONSOLIDATING volumes back to VERIFIED."""
        vols = []
        for i in range(3):
            v = create_volume(
                memory_db, label=f"ABORT_MULTI_{i}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
                status="VERIFIED",
            )
            vols.append(v)

        merger = VolumeMerger(memory_db)
        merger.mark_sources_consolidating([v.volume_id for v in vols])
        merger.abort_consolidation([v.volume_id for v in vols])

        for v in vols:
            updated = get_volume_by_id(memory_db, v.volume_id)
            assert updated.status == "VERIFIED"
