"""Tests for complex cross-table queries."""

from __future__ import annotations

from lcsas.db.queries import (
    get_archive_status_summary,
    get_missing_packs,
    get_packs_for_volume,
    get_packs_only_on_volumes,
    get_pick_list,
    get_redundancy_report,
    get_total_unarchived_bytes,
    get_unarchived_packs,
    get_volumes_for_pack,
)


class TestUnarchived:
    def test_unarchived_packs(self, populated_db):
        """Packs 15-20 are unarchived in the fixture."""
        unarchived = get_unarchived_packs(populated_db)
        sha_set = {p.sha256 for p in unarchived}
        for i in range(15, 21):
            assert f"pack_{i:04d}_hash" in sha_set
        assert len(unarchived) == 6

    def test_unarchived_by_repo(self, populated_db):
        """Filter unarchived by repo_id."""
        unarchived = get_unarchived_packs(populated_db, repo_id="repo_family")
        # Packs 15-20 with repo cycling: 15=family, 18=family
        family_packs = {p.sha256 for p in unarchived}
        # (i-1) % 3: 14%3=2=friend, 15%3=0=family, 16%3=1=work,
        # 17%3=2=friend, 18%3=0=family, 19%3=1=work
        # Family among 15-20: pack_0016 (idx 15), pack_0019 (idx 18)
        assert "pack_0016_hash" in family_packs
        assert "pack_0019_hash" in family_packs

    def test_total_unarchived_bytes(self, populated_db):
        total = get_total_unarchived_bytes(populated_db)
        # Packs 15-20 with sizes 15000...20000
        expected = sum(1000 * i for i in range(15, 21))
        assert total == expected


class TestPackVolumeLookup:
    def test_packs_for_volume(self, populated_db):
        """Vol 1 has packs 1-5."""
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(populated_db, "LCSAS_BD_2026_001")
        packs = get_packs_for_volume(populated_db, vol.volume_id)
        assert len(packs) == 5

    def test_volumes_for_pack(self, populated_db):
        """Pack 1 is on vol 1 and vol 4 (redundant)."""
        from lcsas.db.packs import get_pack_by_sha256
        pack = get_pack_by_sha256(populated_db, "pack_0001_hash")
        vols = get_volumes_for_pack(populated_db, pack.pack_id)
        assert len(vols) == 2
        labels = {v.label for v in vols}
        assert "LCSAS_BD_2026_001" in labels
        assert "LCSAS_MD_2026_001" in labels


class TestPickList:
    def test_basic_pick_list(self, populated_db):
        """Should map packs to their volumes."""
        needed = ["pack_0001_hash", "pack_0006_hash", "pack_0011_hash"]
        pick = get_pick_list(populated_db, needed)
        # Should have entries for at least some volumes
        all_packs = [p for packs in pick.values() for p in packs]
        found_hashes = {p.sha256 for p in all_packs}
        assert needed[0] in found_hashes
        assert needed[1] in found_hashes
        assert needed[2] in found_hashes

    def test_empty_pick_list(self, populated_db):
        pick = get_pick_list(populated_db, [])
        assert pick == {}

    def test_missing_packs(self, populated_db):
        """Packs not in the DB at all should show up as missing."""
        missing = get_missing_packs(populated_db, ["nonexistent_hash"])
        assert "nonexistent_hash" in missing


class TestRedundancy:
    def test_packs_with_single_copy(self, populated_db):
        """Most packs in fixture have only 1 copy; packs 1-3 have 2."""
        under_copies = get_redundancy_report(populated_db, min_copies=2)
        sha_set = {p.sha256 for p in under_copies}
        # Packs 4-14 should be in the report (single copy)
        assert "pack_0004_hash" in sha_set
        assert "pack_0010_hash" in sha_set
        # Packs 1-3 should NOT be (they have 2 copies)
        assert "pack_0001_hash" not in sha_set
        assert "pack_0002_hash" not in sha_set

    def test_all_redundant_with_min_1(self, populated_db):
        """With min_copies=1, only unarchived packs should appear."""
        under = get_redundancy_report(populated_db, min_copies=1)
        sha_set = {p.sha256 for p in under}
        # Unarchived packs (15-20) have 0 copies
        for i in range(15, 21):
            assert f"pack_{i:04d}_hash" in sha_set


class TestConsolidation:
    def test_packs_on_volumes(self, populated_db):
        from lcsas.db.volumes import get_volume_by_label
        vol1 = get_volume_by_label(populated_db, "LCSAS_BD_2026_001")
        vol2 = get_volume_by_label(populated_db, "LCSAS_BD_2026_002")
        packs = get_packs_only_on_volumes(
            populated_db, [vol1.volume_id, vol2.volume_id]
        )
        # Should get packs 1-10 (unique across vol1 and vol2)
        assert len(packs) == 10


class TestStatusSummary:
    def test_summary(self, populated_db):
        summary = get_archive_status_summary(populated_db)
        assert summary["total"] == 20
        assert summary["archived"] == 14
        assert summary["unarchived"] == 6
        assert summary["pruned"] == 0
