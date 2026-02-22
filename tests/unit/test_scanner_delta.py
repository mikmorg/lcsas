"""Tests for pack scanner and delta analysis."""

from __future__ import annotations

from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs
from lcsas.utils.labels import generate_uuid


class TestScanner:
    def test_scan_two_level_layout(self, tmp_mirror):
        """The tmp_mirror fixture uses two-level hash dirs."""
        packs = scan_mirror_packs(tmp_mirror)
        assert len(packs) == 10
        for _sha, size in packs.items():
            assert size > 0

    def test_scan_flat_layout(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        for i in range(5):
            (data_dir / f"flatpack_{i:064x}").write_bytes(b"x" * 50)
        packs = scan_mirror_packs(tmp_path)
        assert len(packs) == 5

    def test_scan_empty_dir(self, tmp_path):
        (tmp_path / "data").mkdir()
        packs = scan_mirror_packs(tmp_path)
        assert packs == {}

    def test_scan_no_data_dir(self, tmp_path):
        packs = scan_mirror_packs(tmp_path)
        assert packs == {}


class TestDeltaAnalyzer:
    def test_register_new_packs(self, memory_db):
        scanner_result = {"hash_a": 100, "hash_b": 200, "hash_c": 300}
        delta = DeltaAnalyzer(memory_db, scanner_result, repo_id="_test")
        new = delta.register_new_packs()
        assert len(new) == 3

    def test_register_skips_existing(self, memory_db):
        register_pack(memory_db, sha256="existing", size_bytes=500, repo_id="_test")
        scanner_result = {"existing": 500, "new_one": 100}
        delta = DeltaAnalyzer(memory_db, scanner_result, repo_id="_test")
        new = delta.register_new_packs()
        assert len(new) == 1
        assert new[0].sha256 == "new_one"

    def test_get_unarchived(self, memory_db):
        register_pack(memory_db, sha256="unarch_1", size_bytes=100, repo_id="_test")
        register_pack(memory_db, sha256="unarch_2", size_bytes=200, repo_id="_test")
        delta = DeltaAnalyzer(memory_db, {})
        unarchived = delta.get_unarchived()
        assert len(unarchived) == 2

    def test_archived_packs_excluded(self, memory_db):
        p = register_pack(memory_db, sha256="archived", size_bytes=100, repo_id="_test")
        vol = create_volume(
            memory_db, label="V1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        bulk_link_packs(memory_db, vol.volume_id, [p.pack_id])

        delta = DeltaAnalyzer(memory_db, {})
        unarchived = delta.get_unarchived()
        assert len(unarchived) == 0

    def test_total_unarchived_bytes(self, memory_db):
        register_pack(memory_db, sha256="u1", size_bytes=100, repo_id="_test")
        register_pack(memory_db, sha256="u2", size_bytes=300, repo_id="_test")
        delta = DeltaAnalyzer(memory_db, {})
        assert delta.get_total_unarchived_bytes() == 400

    def test_needs_burn(self, memory_db):
        register_pack(memory_db, sha256="x1", size_bytes=600_000, repo_id="_test")
        delta = DeltaAnalyzer(memory_db, {})
        assert delta.needs_burn(1_000_000) is False
        assert delta.needs_burn(500_000) is True

    def test_with_repo_filter(self, memory_db):
        register_repo(memory_db, "rfam", "Family", "/fam")
        register_repo(memory_db, "rwrk", "Work", "/wrk")
        register_pack(memory_db, sha256="fam1", size_bytes=100, repo_id="rfam")
        register_pack(memory_db, sha256="wrk1", size_bytes=200, repo_id="rwrk")

        delta_fam = DeltaAnalyzer(memory_db, {}, repo_id="rfam")
        assert len(delta_fam.get_unarchived()) == 1
        assert delta_fam.get_total_unarchived_bytes() == 100

    def test_detect_pruned_finds_missing(self, memory_db):
        """Packs in DB but not on mirror are detected as pruned."""
        p1 = register_pack(memory_db, sha256="keep_me", size_bytes=100, repo_id="_test")
        p2 = register_pack(memory_db, sha256="prune_me", size_bytes=200, repo_id="_test")

        # Mirror only has keep_me
        scanner_result = {"keep_me": 100}
        delta = DeltaAnalyzer(memory_db, scanner_result, repo_id="_test")
        pruned = delta.detect_pruned()

        assert len(pruned) == 1
        assert pruned[0].sha256 == "prune_me"

    def test_detect_pruned_empty_scanner(self, memory_db):
        """Empty scanner result means no prune detection possible."""
        register_pack(memory_db, sha256="some_p", size_bytes=50, repo_id="_test")
        delta = DeltaAnalyzer(memory_db, {}, repo_id="_test")
        assert delta.detect_pruned() == []

    def test_detect_pruned_ignores_already_pruned(self, memory_db):
        """Already-pruned packs are not returned again."""
        from lcsas.db.packs import mark_pruned
        p = register_pack(memory_db, sha256="old_pruned", size_bytes=300, repo_id="_test")
        mark_pruned(memory_db, p.pack_id)

        scanner_result = {"something_else": 500}
        delta = DeltaAnalyzer(memory_db, scanner_result, repo_id="_test")
        pruned = delta.detect_pruned()
        assert len(pruned) == 0


class TestBulkMarkPruned:
    def test_bulk_mark_pruned(self, memory_db):
        from lcsas.db.packs import bulk_mark_pruned, get_pack_by_sha256
        p1 = register_pack(memory_db, sha256="bp1", size_bytes=100, repo_id="_test")
        p2 = register_pack(memory_db, sha256="bp2", size_bytes=200, repo_id="_test")
        p3 = register_pack(memory_db, sha256="bp3", size_bytes=300, repo_id="_test")

        count = bulk_mark_pruned(memory_db, [p1.pack_id, p2.pack_id])
        assert count == 2

        assert get_pack_by_sha256(memory_db, "bp1").is_pruned is True
        assert get_pack_by_sha256(memory_db, "bp2").is_pruned is True
        assert get_pack_by_sha256(memory_db, "bp3").is_pruned is False

    def test_bulk_mark_empty(self, memory_db):
        from lcsas.db.packs import bulk_mark_pruned
        assert bulk_mark_pruned(memory_db, []) == 0

    def test_bulk_mark_idempotent(self, memory_db):
        from lcsas.db.packs import bulk_mark_pruned, mark_pruned
        p = register_pack(memory_db, sha256="already", size_bytes=100, repo_id="_test")
        mark_pruned(memory_db, p.pack_id)

        count = bulk_mark_pruned(memory_db, [p.pack_id])
        assert count == 0  # already pruned, no change
