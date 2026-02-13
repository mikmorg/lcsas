"""Tests for the restore planner."""

from __future__ import annotations

from lcsas.db.packs import register_pack
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.restore.planner import RestorePlanner
from lcsas.utils.labels import generate_uuid


class TestRestorePlanner:
    def test_basic_pick_list(self, memory_db):
        """Create known packs on known volumes, verify pick list."""
        vol = create_volume(
            memory_db, label="V1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        p1 = register_pack(memory_db, sha256="restore_pack_1", size_bytes=1000)
        p2 = register_pack(memory_db, sha256="restore_pack_2", size_bytes=2000)
        bulk_link_packs(memory_db, vol.volume_id, [p1.pack_id, p2.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list(["restore_pack_1", "restore_pack_2"])

        assert "V1" in pick.volumes
        assert len(pick.volumes["V1"]) == 2
        assert pick.total_packs == 2
        assert pick.total_bytes == 3000
        assert pick.missing_packs == []

    def test_missing_packs_detected(self, memory_db):
        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list(["ghost_pack"])

        assert len(pick.missing_packs) == 1
        assert "ghost_pack" in pick.missing_packs

    def test_multi_volume_pick_list(self, memory_db):
        vol1 = create_volume(
            memory_db, label="VA", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        vol2 = create_volume(
            memory_db, label="VB", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        p1 = register_pack(memory_db, sha256="on_va", size_bytes=500)
        p2 = register_pack(memory_db, sha256="on_vb", size_bytes=600)
        bulk_link_packs(memory_db, vol1.volume_id, [p1.pack_id])
        bulk_link_packs(memory_db, vol2.volume_id, [p2.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list(["on_va", "on_vb"])

        assert len(pick.volumes) == 2
        assert pick.total_packs == 2

    def test_empty_request(self, memory_db):
        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list([])
        assert pick.volumes == {}
        assert pick.missing_packs == []

    def test_deprecated_volumes_excluded(self, memory_db):
        vol = create_volume(
            memory_db, label="OLD", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="DEPRECATED",
        )
        p = register_pack(memory_db, sha256="old_pack", size_bytes=100)
        bulk_link_packs(memory_db, vol.volume_id, [p.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list(["old_pack"])
        # Pack exists but its only volume is DEPRECATED
        assert pick.total_packs == 0
