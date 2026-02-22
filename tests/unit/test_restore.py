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
        p1 = register_pack(memory_db, sha256="restore_pack_1", size_bytes=1000, repo_id="_test")
        p2 = register_pack(memory_db, sha256="restore_pack_2", size_bytes=2000, repo_id="_test")
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
        p1 = register_pack(memory_db, sha256="on_va", size_bytes=500, repo_id="_test")
        p2 = register_pack(memory_db, sha256="on_vb", size_bytes=600, repo_id="_test")
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
        p = register_pack(memory_db, sha256="old_pack", size_bytes=100, repo_id="_test")
        bulk_link_packs(memory_db, vol.volume_id, [p.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list(["old_pack"])
        # Pack exists but its only volume is DEPRECATED
        assert pick.total_packs == 0


class TestPickListV2:
    """Tests for PickListV2 with alternate volume information."""

    def test_single_volume_no_alternates(self, memory_db):
        vol = create_volume(
            memory_db, label="V1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="VERIFIED",
        )
        p = register_pack(memory_db, sha256="solo_pack", size_bytes=500, repo_id="_test")
        bulk_link_packs(memory_db, vol.volume_id, [p.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list_v2(["solo_pack"])

        assert "V1" in pick.volumes
        assert len(pick.volumes["V1"]) == 1
        src = pick.volumes["V1"][0]
        assert src.pack.sha256 == "solo_pack"
        assert src.volume_label == "V1"
        assert src.alternates == []

    def test_pack_on_two_volumes_gets_alternates(self, memory_db):
        v1 = create_volume(
            memory_db, label="VA", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="VERIFIED",
        )
        v2 = create_volume(
            memory_db, label="VB", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="VERIFIED",
        )
        p = register_pack(memory_db, sha256="dup_pack", size_bytes=1000, repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, v2.volume_id, [p.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list_v2(["dup_pack"])

        # Pack should be assigned to VA (alphabetical), with VB as alternate
        assert pick.total_packs == 1
        all_sources = [s for sl in pick.volumes.values() for s in sl]
        assert len(all_sources) == 1
        src = all_sources[0]
        assert src.volume_label == "VA"
        assert "VB" in src.alternates

    def test_preferred_location_wins(self, memory_db):
        from lcsas.db.locations import create_location
        create_location(memory_db, "Remote")
        create_location(memory_db, "Home")

        v1 = create_volume(
            memory_db, label="VA", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="VERIFIED",
            location="Remote",
        )
        v2 = create_volume(
            memory_db, label="VB", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="VERIFIED",
            location="Home",
        )
        p = register_pack(memory_db, sha256="loc_pack", size_bytes=800, repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, v2.volume_id, [p.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list_v2(["loc_pack"], preferred_location="Home")

        all_sources = [s for sl in pick.volumes.values() for s in sl]
        src = all_sources[0]
        assert src.volume_label == "VB"  # Home preferred
        assert "VA" in src.alternates

    def test_verified_preferred_over_burned(self, memory_db):
        v1 = create_volume(
            memory_db, label="VA", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="BURNED",
        )
        v2 = create_volume(
            memory_db, label="VB", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25e9, status="VERIFIED",
        )
        p = register_pack(memory_db, sha256="pref_pack", size_bytes=100, repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, v2.volume_id, [p.pack_id])

        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list_v2(["pref_pack"])

        all_sources = [s for sl in pick.volumes.values() for s in sl]
        src = all_sources[0]
        assert src.volume_label == "VB"  # VERIFIED beats BURNED

    def test_empty_pick_list_v2(self, memory_db):
        planner = RestorePlanner(memory_db)
        pick = planner.generate_pick_list_v2([])
        assert pick.volumes == {}
        assert pick.missing_packs == []
        assert pick.total_packs == 0
