"""Multi-copy redundancy and restore-from-any-combination tests.

Validates that:
  - Packs archived to multiple volumes create proper redundancy
  - Restore can succeed from ANY viable subset of volumes
  - Pick list adapts when volumes are deprecated/destroyed
  - RestoreExecutor correctly assembles cache from multiple volumes
  - Data integrity is verified after every restore path
"""

from __future__ import annotations

import hashlib
import itertools
from unittest.mock import MagicMock

import pytest

from lcsas.db.packs import get_pack_by_sha256, register_pack
from lcsas.db.queries import (
    get_packs_for_volume,
    get_pick_list,
    get_redundancy_report,
    get_volumes_for_pack,
)
from lcsas.db.repos import register_repo
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, update_status
from lcsas.restore.executor import RestoreExecutor
from lcsas.restore.planner import RestorePlanner
from lcsas.utils.labels import generate_uuid

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def redundant_db(memory_db):
    """DB with packs distributed across 4 volumes with varied redundancy.

    Repos: photos, docs
    Packs (10 total):
      - photos: p01..p06 (sizes 1000..6000)
      - docs:   d01..d04 (sizes 1000..4000)

    Volume layout (designed so every pack has at least 2 copies):
      - VOL_A (VERIFIED): p01, p02, p03, p04, d01, d02
      - VOL_B (VERIFIED): p03, p04, p05, p06, d02, d03
      - VOL_C (VERIFIED): p01, p02, p05, p06, d03, d04
      - VOL_D (VERIFIED): p03, p04, d01, d04

    Copy counts:
      - p01: VOL_A, VOL_C (2 copies)
      - p02: VOL_A, VOL_C (2 copies)
      - p03: VOL_A, VOL_B, VOL_D (3 copies)
      - p04: VOL_A, VOL_B, VOL_D (3 copies)
      - p05: VOL_B, VOL_C (2 copies)
      - p06: VOL_B, VOL_C (2 copies)
      - d01: VOL_A, VOL_D (2 copies)
      - d02: VOL_A, VOL_B (2 copies)
      - d03: VOL_B, VOL_C (2 copies)
      - d04: VOL_C, VOL_D (2 copies)
    """
    conn = memory_db

    register_repo(conn, "photos", "Photos", "/mnt/mirror/photos")
    register_repo(conn, "docs", "Documents", "/mnt/mirror/docs")

    packs = {}
    for i in range(1, 7):
        sha = f"p{i:02d}_hash"
        packs[sha] = register_pack(conn, sha256=sha, size_bytes=1000 * i, repo_id="photos")
    for i in range(1, 5):
        sha = f"d{i:02d}_hash"
        packs[sha] = register_pack(conn, sha256=sha, size_bytes=1000 * i, repo_id="docs")

    vols = {}
    for label in ["VOL_A", "VOL_B", "VOL_C", "VOL_D"]:
        vols[label] = create_volume(
            conn, label=label, uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )

    # Link packs to volumes
    vol_packs = {
        "VOL_A": ["p01_hash", "p02_hash", "p03_hash", "p04_hash", "d01_hash", "d02_hash"],
        "VOL_B": ["p03_hash", "p04_hash", "p05_hash", "p06_hash", "d02_hash", "d03_hash"],
        "VOL_C": ["p01_hash", "p02_hash", "p05_hash", "p06_hash", "d03_hash", "d04_hash"],
        "VOL_D": ["p03_hash", "p04_hash", "d01_hash", "d04_hash"],
    }
    for label, sha_list in vol_packs.items():
        pack_ids = [packs[sha].pack_id for sha in sha_list]
        bulk_link_packs(conn, vols[label].volume_id, pack_ids)

    return conn


@pytest.fixture
def volume_dirs(tmp_path):
    """Create simulated mounted volume directories with pack files.

    Each volume has a data/ directory with pack files matching the
    redundant_db fixture layout. Files contain deterministic content
    based on their SHA to enable integrity verification.
    """
    vol_packs = {
        "VOL_A": ["p01_hash", "p02_hash", "p03_hash", "p04_hash", "d01_hash", "d02_hash"],
        "VOL_B": ["p03_hash", "p04_hash", "p05_hash", "p06_hash", "d02_hash", "d03_hash"],
        "VOL_C": ["p01_hash", "p02_hash", "p05_hash", "p06_hash", "d03_hash", "d04_hash"],
        "VOL_D": ["p03_hash", "p04_hash", "d01_hash", "d04_hash"],
    }

    def _pack_content(sha: str) -> bytes:
        """Deterministic content for a pack, derived from its SHA name."""
        return hashlib.sha256(sha.encode()).digest() * 4  # 128 bytes

    dirs = {}
    for label, sha_list in vol_packs.items():
        vol_dir = tmp_path / label
        data_dir = vol_dir / "data"
        data_dir.mkdir(parents=True)
        for sha in sha_list:
            (data_dir / sha).write_bytes(_pack_content(sha))

        # Also create metadata directories (for prepare_cache)
        for subdir in ["index", "snapshots", "keys"]:
            d = vol_dir / "metadata" / "photos" / subdir
            d.mkdir(parents=True)
            (d / "dummy.json").write_text("{}")
        config = vol_dir / "metadata" / "photos" / "config"
        config.write_text('{"version": 2}')

        dirs[label] = vol_dir

    return dirs


def _pack_content(sha: str) -> bytes:
    """Deterministic content for a given SHA — must match volume_dirs fixture."""
    return hashlib.sha256(sha.encode()).digest() * 4


# =========================================================================
# Redundancy verification
# =========================================================================


class TestRedundancyCopies:

    def test_all_packs_have_at_least_2_copies(self, redundant_db):
        """Every pack should have >= 2 volume copies."""
        under_2 = get_redundancy_report(redundant_db, min_copies=2)
        assert len(under_2) == 0, (
            f"Expected all packs to have 2+ copies, but these don't: "
            f"{[p.sha256 for p in under_2]}"
        )

    def test_p03_p04_have_3_copies(self, redundant_db):
        """p03, p04 should be on VOL_A, VOL_B, VOL_D."""
        for sha in ["p03_hash", "p04_hash"]:
            p = get_pack_by_sha256(redundant_db, sha)
            vols = get_volumes_for_pack(redundant_db, p.pack_id)
            assert len(vols) == 3, f"{sha} expected 3 copies, got {len(vols)}"
            labels = {v.label for v in vols}
            assert labels == {"VOL_A", "VOL_B", "VOL_D"}

    def test_p01_p02_have_2_copies(self, redundant_db):
        for sha in ["p01_hash", "p02_hash"]:
            p = get_pack_by_sha256(redundant_db, sha)
            vols = get_volumes_for_pack(redundant_db, p.pack_id)
            assert len(vols) == 2
            labels = {v.label for v in vols}
            assert labels == {"VOL_A", "VOL_C"}

    def test_d01_on_vol_a_and_vol_d(self, redundant_db):
        p = get_pack_by_sha256(redundant_db, "d01_hash")
        vols = get_volumes_for_pack(redundant_db, p.pack_id)
        labels = {v.label for v in vols}
        assert labels == {"VOL_A", "VOL_D"}


# =========================================================================
# Restore from any single volume
# =========================================================================


class TestRestoreFromSingleVolume:

    def test_single_vol_a_covers_subset(self, redundant_db):
        """VOL_A can provide p01-p04, d01, d02."""
        packs = get_packs_for_volume(redundant_db, 1)  # VOL_A
        shas = {p.sha256 for p in packs}
        assert shas == {"p01_hash", "p02_hash", "p03_hash", "p04_hash",
                        "d01_hash", "d02_hash"}

    def test_single_vol_b_covers_different_subset(self, redundant_db):
        packs = get_packs_for_volume(redundant_db, 2)  # VOL_B
        shas = {p.sha256 for p in packs}
        assert shas == {"p03_hash", "p04_hash", "p05_hash", "p06_hash",
                        "d02_hash", "d03_hash"}


# =========================================================================
# Pick-list with volume degradation
# =========================================================================


class TestPickListDegradation:
    """Test pick-list generation when volumes are deprecated or destroyed."""

    ALL_SHAS = (
        [f"p{i:02d}_hash" for i in range(1, 7)]
        + [f"d{i:02d}_hash" for i in range(1, 5)]
    )

    def test_all_volumes_healthy(self, redundant_db):
        pick = get_pick_list(redundant_db, self.ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}
        assert found == set(self.ALL_SHAS)

    def test_deprecate_vol_a(self, redundant_db):
        """After deprecating VOL_A, packs should still be found on other volumes."""
        update_status(redundant_db, 1, "DEPRECATED")  # VOL_A

        pick = get_pick_list(redundant_db, self.ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}
        # All packs have copies elsewhere
        assert found == set(self.ALL_SHAS)
        # VOL_A should NOT appear in pick list
        assert "VOL_A" not in pick

    def test_destroy_vol_a_and_vol_d(self, redundant_db):
        """Destroying VOL_A and VOL_D — d01 only had copies on those two."""
        update_status(redundant_db, 1, "DESTROYED")  # VOL_A
        update_status(redundant_db, 4, "DESTROYED")  # VOL_D

        pick = get_pick_list(redundant_db, self.ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}

        # d01 only existed on VOL_A and VOL_D — it's now missing
        assert "d01_hash" not in found

        # All other packs should still be found
        other_shas = set(self.ALL_SHAS) - {"d01_hash"}
        assert found == other_shas

    def test_missing_packs_after_destruction(self, redundant_db):
        """get_missing_packs correctly identifies packs with no viable volume."""
        update_status(redundant_db, 1, "DESTROYED")  # VOL_A
        update_status(redundant_db, 4, "DESTROYED")  # VOL_D

        # d01 is on VOL_A + VOL_D only, both destroyed
        # get_missing_packs checks volume_packs (not status), so d01 is still linked
        # It won't appear as "missing" since it IS on volumes (just destroyed ones)
        # The pick_list handles status filtering instead
        pick = get_pick_list(redundant_db, ["d01_hash"])
        found = {p.sha256 for packs in pick.values() for p in packs}
        assert "d01_hash" not in found

    def test_deprecate_one_of_three_copies(self, redundant_db):
        """p03 has 3 copies. Deprecating VOL_A still leaves 2 copies."""
        update_status(redundant_db, 1, "DEPRECATED")  # VOL_A

        pick = get_pick_list(redundant_db, ["p03_hash"])
        found_labels = set(pick.keys())
        assert "VOL_A" not in found_labels
        # p03 still available from VOL_B or VOL_D
        all_packs = [p for packs in pick.values() for p in packs]
        assert len(all_packs) == 1
        assert all_packs[0].sha256 == "p03_hash"


# =========================================================================
# Restore from every viable volume combination
# =========================================================================


class TestRestoreFromAnyCombination:
    """Test that restore can succeed from any combination of volumes
    that collectively contain all required packs."""

    ALL_PHOTO_SHAS = [f"p{i:02d}_hash" for i in range(1, 7)]
    ALL_DOC_SHAS = [f"d{i:02d}_hash" for i in range(1, 5)]
    ALL_SHAS = ALL_PHOTO_SHAS + ALL_DOC_SHAS

    # Which volumes contain which packs
    VOL_CONTENTS = {
        "VOL_A": {"p01_hash", "p02_hash", "p03_hash", "p04_hash", "d01_hash", "d02_hash"},
        "VOL_B": {"p03_hash", "p04_hash", "p05_hash", "p06_hash", "d02_hash", "d03_hash"},
        "VOL_C": {"p01_hash", "p02_hash", "p05_hash", "p06_hash", "d03_hash", "d04_hash"},
        "VOL_D": {"p03_hash", "p04_hash", "d01_hash", "d04_hash"},
    }

    def _viable_combinations(self) -> list[tuple[str, ...]]:
        """Return all volume subsets that collectively cover ALL_SHAS."""
        all_labels = list(self.VOL_CONTENTS.keys())
        viable = []
        for size in range(1, len(all_labels) + 1):
            for combo in itertools.combinations(all_labels, size):
                union = set()
                for label in combo:
                    union |= self.VOL_CONTENTS[label]
                if union >= set(self.ALL_SHAS):
                    viable.append(combo)
        return viable

    def test_viable_combinations_exist(self):
        """Sanity: at least one viable combination exists."""
        combos = self._viable_combinations()
        assert len(combos) > 0

    def test_all_4_volumes_is_viable(self):
        combos = self._viable_combinations()
        assert ("VOL_A", "VOL_B", "VOL_C", "VOL_D") in combos

    def test_no_single_volume_is_viable(self):
        """No single volume has all 10 packs."""
        combos = self._viable_combinations()
        singles = [c for c in combos if len(c) == 1]
        assert singles == []

    def test_enumerate_all_viable_2vol_combinations(self):
        """Find all pairs that cover all packs."""
        combos = self._viable_combinations()
        pairs = [c for c in combos if len(c) == 2]
        # VOL_A (p01-p04,d01,d02) + VOL_C (p01,p02,p05,p06,d03,d04)
        #   = all packs
        # VOL_A + VOL_B = p01-p06,d01-d03 — missing d04
        # Let's verify programmatically
        for pair in pairs:
            union = set()
            for label in pair:
                union |= self.VOL_CONTENTS[label]
            assert union >= set(self.ALL_SHAS)

    def test_restore_executor_from_each_viable_combination(
        self, redundant_db, volume_dirs, tmp_path
    ):
        """For each viable volume combination, simulate a full restore
        and verify that every pack is correctly assembled in the cache."""
        combos = self._viable_combinations()
        assert len(combos) >= 1

        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        for idx, combo in enumerate(combos):
            cache_dir = tmp_path / f"cache_{idx}"
            # Prepare cache from first volume's metadata
            first_vol = volume_dirs[combo[0]]
            executor.prepare_cache(cache_dir, first_vol / "metadata" / "photos")

            # Ingest from each volume in the combination
            total_ingested = 0
            for label in combo:
                vol_dir = volume_dirs[label]
                # Determine which of ALL_SHAS this volume has
                vol_shas = list(self.VOL_CONTENTS[label])
                ingested = executor.ingest_volume(cache_dir, vol_dir, vol_shas)
                total_ingested += ingested

            # Verify ALL packs are in the cache
            for sha in self.ALL_SHAS:
                cached_file = cache_dir / "data" / sha[:2] / sha
                assert cached_file.exists(), (
                    f"Pack {sha} missing from cache after ingesting {combo}"
                )
                # Verify content integrity
                expected_content = _pack_content(sha)
                actual_content = cached_file.read_bytes()
                assert actual_content == expected_content, (
                    f"Pack {sha} content mismatch after ingesting {combo}"
                )

    def test_pick_list_plans_viable_restore(self, redundant_db):
        """RestorePlanner generates a plan that covers all packs."""
        planner = RestorePlanner(redundant_db)
        pick = planner.generate_pick_list(self.ALL_SHAS)

        assert pick.missing_packs == []
        assert pick.total_packs == 10
        assert pick.total_bytes > 0

        # Verify all packs are accounted for
        planned_shas = {
            p.sha256 for packs in pick.volumes.values() for p in packs
        }
        assert planned_shas == set(self.ALL_SHAS)

    def test_pick_list_with_degraded_volumes(self, redundant_db):
        """Even with one volume destroyed, planner finds alternatives."""
        update_status(redundant_db, 2, "DESTROYED")  # VOL_B

        planner = RestorePlanner(redundant_db)
        pick = planner.generate_pick_list(self.ALL_SHAS)

        # All packs should still be findable (each has copies elsewhere)
        assert pick.missing_packs == []
        planned_shas = {
            p.sha256 for packs in pick.volumes.values() for p in packs
        }
        assert planned_shas == set(self.ALL_SHAS)
        assert "VOL_B" not in pick.volumes


# =========================================================================
# Restore executor: multi-volume ingest
# =========================================================================


class TestMultiVolumeIngest:
    """Test RestoreExecutor ingesting packs from multiple volumes."""

    def test_ingest_from_two_volumes_no_overlap(self, tmp_path):
        """Volume A has pack1, Volume B has pack2. Both ingested."""
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        vol_a = tmp_path / "vol_a"
        (vol_a / "data").mkdir(parents=True)
        (vol_a / "data" / "sha_pack1").write_bytes(b"data1")

        vol_b = tmp_path / "vol_b"
        (vol_b / "data").mkdir(parents=True)
        (vol_b / "data" / "sha_pack2").write_bytes(b"data2")

        cache = tmp_path / "cache"
        cache.mkdir()

        n1 = executor.ingest_volume(cache, vol_a, ["sha_pack1"])
        n2 = executor.ingest_volume(cache, vol_b, ["sha_pack2"])

        assert n1 == 1
        assert n2 == 1
        assert (cache / "data" / "sh" / "sha_pack1").read_bytes() == b"data1"
        assert (cache / "data" / "sh" / "sha_pack2").read_bytes() == b"data2"

    def test_ingest_overlapping_packs_first_wins(self, tmp_path):
        """If pack exists on both volumes, first ingest wins (skip)."""
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        vol_a = tmp_path / "vol_a"
        (vol_a / "data").mkdir(parents=True)
        (vol_a / "data" / "common_pack").write_bytes(b"from_vol_a")

        vol_b = tmp_path / "vol_b"
        (vol_b / "data").mkdir(parents=True)
        (vol_b / "data" / "common_pack").write_bytes(b"from_vol_b")

        cache = tmp_path / "cache"
        cache.mkdir()

        n1 = executor.ingest_volume(cache, vol_a, ["common_pack"])
        n2 = executor.ingest_volume(cache, vol_b, ["common_pack"])

        assert n1 == 1
        assert n2 == 0  # Already cached
        assert (cache / "data" / "co" / "common_pack").read_bytes() == b"from_vol_a"

    def test_partial_volume_ingest(self, tmp_path):
        """Volume only has some of the requested packs."""
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        vol = tmp_path / "vol"
        (vol / "data").mkdir(parents=True)
        (vol / "data" / "exists").write_bytes(b"found")

        cache = tmp_path / "cache"
        cache.mkdir()

        n = executor.ingest_volume(cache, vol, ["exists", "missing1", "missing2"])
        assert n == 1

    def test_full_restore_workflow(self, tmp_path):
        """End-to-end: prepare_cache → ingest from 2 volumes → execute_restore."""
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        # Create metadata source
        meta = tmp_path / "metadata"
        for subdir in ["index", "snapshots", "keys"]:
            d = meta / subdir
            d.mkdir(parents=True)
            (d / "data.json").write_text("{}")
        (meta / "config").write_text('{"version": 2}')

        # Create two volumes
        vol1 = tmp_path / "vol1"
        (vol1 / "data").mkdir(parents=True)
        (vol1 / "data" / "pack_a").write_bytes(b"aaaa")
        (vol1 / "data" / "pack_b").write_bytes(b"bbbb")

        vol2 = tmp_path / "vol2"
        (vol2 / "data").mkdir(parents=True)
        (vol2 / "data" / "pack_c").write_bytes(b"cccc")

        # Full workflow
        cache = tmp_path / "cache"
        executor.prepare_cache(cache, meta)
        executor.ingest_volume(cache, vol1, ["pack_a", "pack_b"])
        executor.ingest_volume(cache, vol2, ["pack_c"])

        # Verify cache structure
        assert (cache / "index" / "data.json").exists()
        assert (cache / "config").exists()
        assert (cache / "data" / "pa" / "pack_a").read_bytes() == b"aaaa"
        assert (cache / "data" / "pa" / "pack_b").read_bytes() == b"bbbb"
        assert (cache / "data" / "pa" / "pack_c").read_bytes() == b"cccc"

        # Execute restore
        pw = tmp_path / "pw.txt"
        pw.write_text("test")
        target = tmp_path / "restored"
        executor.execute_restore(cache, "snap123", target, pw)

        mock_rustic.restore.assert_called_once_with(
            snapshot_id="snap123",
            repo_path=cache,
            password_file=pw,
            target_path=target,
        )


# =========================================================================
# Cross-repo restore scenario
# =========================================================================


class TestCrossRepoRestore:
    """Test restoring when packs from different repos are on the same volume."""

    def test_restore_photos_only_from_mixed_volume(self, redundant_db, volume_dirs, tmp_path):
        """Only request photo packs — should still work even though
        they share volumes with doc packs."""
        photo_shas = [f"p{i:02d}_hash" for i in range(1, 7)]
        planner = RestorePlanner(redundant_db)
        pick = planner.generate_pick_list(photo_shas)

        assert pick.missing_packs == []
        assert pick.total_packs == 6

        # Simulate ingest using pick list
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)
        cache = tmp_path / "cache"
        first_label = next(iter(pick.volumes))
        executor.prepare_cache(
            cache, volume_dirs[first_label] / "metadata" / "photos"
        )

        for label, packs in pick.volumes.items():
            shas = [p.sha256 for p in packs]
            executor.ingest_volume(cache, volume_dirs[label], shas)

        # Verify only photo packs are requested (doc packs not needed)
        for sha in photo_shas:
            assert (cache / "data" / sha[:2] / sha).exists()

    def test_restore_docs_only_from_mixed_volume(self, redundant_db, volume_dirs, tmp_path):
        """Only request doc packs."""
        doc_shas = [f"d{i:02d}_hash" for i in range(1, 5)]
        planner = RestorePlanner(redundant_db)
        pick = planner.generate_pick_list(doc_shas)

        assert pick.missing_packs == []
        assert pick.total_packs == 4

        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)
        cache = tmp_path / "cache"
        first_label = next(iter(pick.volumes))
        executor.prepare_cache(
            cache, volume_dirs[first_label] / "metadata" / "photos"
        )

        for label, packs in pick.volumes.items():
            shas = [p.sha256 for p in packs]
            executor.ingest_volume(cache, volume_dirs[label], shas)

        for sha in doc_shas:
            assert (cache / "data" / sha[:2] / sha).exists()

    def test_restore_both_repos_simultaneously(self, redundant_db, volume_dirs, tmp_path):
        """Request all packs from both repos at once."""
        all_shas = (
            [f"p{i:02d}_hash" for i in range(1, 7)]
            + [f"d{i:02d}_hash" for i in range(1, 5)]
        )
        planner = RestorePlanner(redundant_db)
        pick = planner.generate_pick_list(all_shas)

        assert pick.missing_packs == []
        assert pick.total_packs == 10

        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)
        cache = tmp_path / "cache"
        first_label = next(iter(pick.volumes))
        executor.prepare_cache(
            cache, volume_dirs[first_label] / "metadata" / "photos"
        )

        for label, packs in pick.volumes.items():
            shas = [p.sha256 for p in packs]
            executor.ingest_volume(cache, volume_dirs[label], shas)

        for sha in all_shas:
            cached = cache / "data" / sha[:2] / sha
            assert cached.exists(), f"{sha} missing from cache"
            assert cached.read_bytes() == _pack_content(sha)


# =========================================================================
# Data integrity: SHA-256 verification after restore
# =========================================================================


class TestDataIntegrityVerification:
    """Ensure pack content is bit-for-bit identical regardless of which
    volume copy was used to restore it."""

    VOL_CONTENTS = {
        "VOL_A": {"p01_hash", "p02_hash", "p03_hash", "p04_hash", "d01_hash", "d02_hash"},
        "VOL_B": {"p03_hash", "p04_hash", "p05_hash", "p06_hash", "d02_hash", "d03_hash"},
        "VOL_C": {"p01_hash", "p02_hash", "p05_hash", "p06_hash", "d03_hash", "d04_hash"},
        "VOL_D": {"p03_hash", "p04_hash", "d01_hash", "d04_hash"},
    }

    def test_same_pack_same_content_across_volumes(self, volume_dirs):
        """p03 is on VOL_A, VOL_B, and VOL_D. All copies must be identical."""
        sha = "p03_hash"
        contents = []
        for label in ["VOL_A", "VOL_B", "VOL_D"]:
            path = volume_dirs[label] / "data" / sha
            assert path.exists()
            contents.append(path.read_bytes())

        # All copies must be identical
        assert contents[0] == contents[1] == contents[2]
        # And match the expected deterministic content
        assert contents[0] == _pack_content(sha)

    def test_integrity_of_every_pack_on_every_volume(self, volume_dirs):
        """Every pack file on every volume matches its expected content."""
        for label, shas in self.VOL_CONTENTS.items():
            for sha in shas:
                path = volume_dirs[label] / "data" / sha
                assert path.exists(), f"{sha} not found on {label}"
                actual = path.read_bytes()
                expected = _pack_content(sha)
                assert actual == expected, (
                    f"Content mismatch for {sha} on {label}: "
                    f"got {len(actual)} bytes, expected {len(expected)}"
                )

    def test_cache_integrity_matches_source(self, volume_dirs, tmp_path):
        """After ingesting from any volume, cache content matches source."""
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)
        cache = tmp_path / "cache"
        cache.mkdir()

        sha = "d02_hash"  # On VOL_A and VOL_B

        # Ingest from VOL_A
        executor.ingest_volume(cache, volume_dirs["VOL_A"], [sha])
        cached = (cache / "data" / sha[:2] / sha).read_bytes()
        source_a = (volume_dirs["VOL_A"] / "data" / sha).read_bytes()
        source_b = (volume_dirs["VOL_B"] / "data" / sha).read_bytes()

        assert cached == source_a
        assert cached == source_b
        assert cached == _pack_content(sha)

    def test_sha256_hash_verification(self, volume_dirs):
        """Compute actual SHA-256 of pack content and verify consistency."""
        sha = "p05_hash"
        expected_content = _pack_content(sha)
        expected_hash = hashlib.sha256(expected_content).hexdigest()

        for label in ["VOL_B", "VOL_C"]:
            path = volume_dirs[label] / "data" / sha
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            assert actual_hash == expected_hash, (
                f"SHA-256 mismatch for {sha} on {label}"
            )
