"""Tests for pack scanner and delta analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

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
        for sha, size in packs.items():
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
        delta = DeltaAnalyzer(memory_db, scanner_result)
        new = delta.register_new_packs()
        assert len(new) == 3

    def test_register_skips_existing(self, memory_db):
        register_pack(memory_db, sha256="existing", size_bytes=500)
        scanner_result = {"existing": 500, "new_one": 100}
        delta = DeltaAnalyzer(memory_db, scanner_result)
        new = delta.register_new_packs()
        assert len(new) == 1
        assert new[0].sha256 == "new_one"

    def test_get_unarchived(self, memory_db):
        register_pack(memory_db, sha256="unarch_1", size_bytes=100)
        register_pack(memory_db, sha256="unarch_2", size_bytes=200)
        delta = DeltaAnalyzer(memory_db, {})
        unarchived = delta.get_unarchived()
        assert len(unarchived) == 2

    def test_archived_packs_excluded(self, memory_db):
        p = register_pack(memory_db, sha256="archived", size_bytes=100)
        vol = create_volume(
            memory_db, label="V1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        bulk_link_packs(memory_db, vol.volume_id, [p.pack_id])

        delta = DeltaAnalyzer(memory_db, {})
        unarchived = delta.get_unarchived()
        assert len(unarchived) == 0

    def test_total_unarchived_bytes(self, memory_db):
        register_pack(memory_db, sha256="u1", size_bytes=100)
        register_pack(memory_db, sha256="u2", size_bytes=300)
        delta = DeltaAnalyzer(memory_db, {})
        assert delta.get_total_unarchived_bytes() == 400

    def test_needs_burn(self, memory_db):
        register_pack(memory_db, sha256="x1", size_bytes=600_000)
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
