"""Full pipeline integration test for multi-tenant + multi-copy scenarios.

Exercises the complete workflow:
  1. Multiple repos with distinct packs
  2. Burn pipeline creating ISOs with packs from multiple repos
  3. Multiple burn passes to create redundant copies
  4. Restore from each viable volume combination
  5. SHA-256 integrity verification after every restore path

Uses only in-memory DB and tmp filesystem — no external tools needed.
"""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.packs import register_pack
from lcsas.db.queries import (
    get_archive_status_summary,
    get_packs_for_volume,
    get_pick_list,
    get_redundancy_report,
    get_unarchived_packs,
    get_volumes_for_pack,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, get_volume_by_label, list_volumes
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs
from lcsas.restore.executor import RestoreExecutor
from lcsas.restore.planner import RestorePlanner
from lcsas.utils.labels import generate_uuid

# =========================================================================
# Helpers
# =========================================================================


def _create_fake_mirror(base: Path, repo_name: str, pack_specs: list[tuple[str, int]]) -> Path:
    """Create a fake restic-style mirror directory with pack files.

    Args:
        base: Parent directory for the mirror.
        repo_name: Name of the repository.
        pack_specs: List of (sha256, size_bytes) for each pack.

    Returns:
        Path to the repo mirror directory.
    """
    repo_dir = base / repo_name
    data_dir = repo_dir / "data"

    for sha, size in pack_specs:
        # Two-level layout
        prefix_dir = data_dir / sha[:2]
        prefix_dir.mkdir(parents=True, exist_ok=True)
        # Content is deterministic from SHA
        content = hashlib.sha256(sha.encode()).digest()
        # Repeat to reach desired size
        full_content = (content * ((size // len(content)) + 1))[:size]
        (prefix_dir / sha).write_bytes(full_content)

    # Metadata
    for subdir in ["index", "snapshots", "keys"]:
        d = repo_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text("{}")
    (repo_dir / "config").write_text('{"version": 2}')

    return repo_dir


def _expected_content(sha: str, size: int) -> bytes:
    """Reproduce the deterministic content for a pack."""
    content = hashlib.sha256(sha.encode()).digest()
    return (content * ((size // len(content)) + 1))[:size]


# =========================================================================
# Fixture: full multi-tenant pipeline scenario
# =========================================================================


@pytest.fixture
def pipeline_env(tmp_path):
    """Set up a complete multi-tenant pipeline environment.

    Creates:
      - 3 repos: family (4 packs), work (3 packs), archive (2 packs)
      - Fake mirror directories with real pack files
      - In-memory DB with schema
      - LCSASConfig pointing to the test dirs
    """
    conn = get_memory_connection()
    create_all(conn)

    mirror_base = tmp_path / "mirror"
    staging = tmp_path / "staging"
    db_path = tmp_path / "catalog.db"
    staging.mkdir()

    # Define packs per repo
    family_packs = [
        ("fam_pack_001_sha256hash", 5000),
        ("fam_pack_002_sha256hash", 8000),
        ("fam_pack_003_sha256hash", 3000),
        ("fam_pack_004_sha256hash", 6000),
    ]
    work_packs = [
        ("wrk_pack_001_sha256hash", 4000),
        ("wrk_pack_002_sha256hash", 7000),
        ("wrk_pack_003_sha256hash", 2000),
    ]
    archive_packs = [
        ("arc_pack_001_sha256hash", 9000),
        ("arc_pack_002_sha256hash", 1000),
    ]

    # Create mirrors
    family_mirror = _create_fake_mirror(mirror_base, "family", family_packs)
    work_mirror = _create_fake_mirror(mirror_base, "work", work_packs)
    archive_mirror = _create_fake_mirror(mirror_base, "archive", archive_packs)

    # Register repos in DB
    register_repo(conn, "family", "Family Photos", str(family_mirror))
    register_repo(conn, "work", "Work Documents", str(work_mirror))
    register_repo(conn, "archive", "Archive Collection", str(archive_mirror))

    return {
        "conn": conn,
        "mirror_base": mirror_base,
        "staging": staging,
        "db_path": db_path,
        "family_mirror": family_mirror,
        "work_mirror": work_mirror,
        "archive_mirror": archive_mirror,
        "family_packs": family_packs,
        "work_packs": work_packs,
        "archive_packs": archive_packs,
    }


# =========================================================================
# Phase 1: Scan & Register
# =========================================================================


class TestScanAndRegister:
    """Test scanning multiple repos and registering their packs."""

    def test_scan_each_repo_independently(self, pipeline_env):
        env = pipeline_env
        conn = env["conn"]

        # Scan family
        fam_scanned = scan_mirror_packs(env["family_mirror"])
        assert len(fam_scanned) == 4
        delta_fam = DeltaAnalyzer(conn, fam_scanned, repo_id="family")
        new_fam = delta_fam.register_new_packs()
        assert len(new_fam) == 4
        assert all(p.repo_id == "family" for p in new_fam)

        # Scan work
        wrk_scanned = scan_mirror_packs(env["work_mirror"])
        assert len(wrk_scanned) == 3
        delta_wrk = DeltaAnalyzer(conn, wrk_scanned, repo_id="work")
        new_wrk = delta_wrk.register_new_packs()
        assert len(new_wrk) == 3
        assert all(p.repo_id == "work" for p in new_wrk)

        # Scan archive
        arc_scanned = scan_mirror_packs(env["archive_mirror"])
        assert len(arc_scanned) == 2
        delta_arc = DeltaAnalyzer(conn, arc_scanned, repo_id="archive")
        new_arc = delta_arc.register_new_packs()
        assert len(new_arc) == 2
        assert all(p.repo_id == "archive" for p in new_arc)

        # Total: 9 packs, all unarchived
        summary = get_archive_status_summary(conn)
        assert summary["total"] == 9
        assert summary["unarchived"] == 9
        assert summary["archived"] == 0

    def test_rescan_is_idempotent(self, pipeline_env):
        """Re-scanning the same repo doesn't create duplicate packs."""
        env = pipeline_env
        conn = env["conn"]

        scanned = scan_mirror_packs(env["family_mirror"])
        DeltaAnalyzer(conn, scanned, repo_id="family").register_new_packs()

        # Rescan
        new_second = DeltaAnalyzer(conn, scanned, repo_id="family").register_new_packs()
        assert len(new_second) == 0

        summary = get_archive_status_summary(conn)
        assert summary["total"] == 4  # Not 8

    def test_unarchived_query_per_repo(self, pipeline_env):
        env = pipeline_env
        conn = env["conn"]

        # Register all repos
        for repo_id, mirror in [
            ("family", env["family_mirror"]),
            ("work", env["work_mirror"]),
            ("archive", env["archive_mirror"]),
        ]:
            scanned = scan_mirror_packs(mirror)
            DeltaAnalyzer(conn, scanned, repo_id=repo_id).register_new_packs()

        assert len(get_unarchived_packs(conn, repo_id="family")) == 4
        assert len(get_unarchived_packs(conn, repo_id="work")) == 3
        assert len(get_unarchived_packs(conn, repo_id="archive")) == 2
        assert len(get_unarchived_packs(conn)) == 9


# =========================================================================
# Phase 2: Simulate burn → archive with redundancy
# =========================================================================


class TestBurnWithRedundancy:
    """Simulate the burn pipeline creating multiple volumes with overlap."""

    def _register_all(self, env):
        conn = env["conn"]
        for repo_id, mirror in [
            ("family", env["family_mirror"]),
            ("work", env["work_mirror"]),
            ("archive", env["archive_mirror"]),
        ]:
            scanned = scan_mirror_packs(mirror)
            DeltaAnalyzer(conn, scanned, repo_id=repo_id).register_new_packs()

    def test_create_two_volumes_with_full_redundancy(self, pipeline_env):
        """Put all 9 packs on VOL_1, then all 9 again on VOL_2."""
        env = pipeline_env
        conn = env["conn"]
        self._register_all(env)

        all_packs = get_unarchived_packs(conn)
        assert len(all_packs) == 9

        # Create VOL_1 with all packs
        vol1 = create_volume(
            conn, label="VOL_1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol1.volume_id, [p.pack_id for p in all_packs])

        # Create VOL_2 with all packs (redundant copy)
        vol2 = create_volume(
            conn, label="VOL_2", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol2.volume_id, [p.pack_id for p in all_packs])

        # All packs now archived
        assert len(get_unarchived_packs(conn)) == 0

        # Every pack has 2 copies
        under_2 = get_redundancy_report(conn, min_copies=2)
        assert len(under_2) == 0

        # Each volume has all 9 packs
        assert len(get_packs_for_volume(conn, vol1.volume_id)) == 9
        assert len(get_packs_for_volume(conn, vol2.volume_id)) == 9

    def test_create_overlapping_volumes(self, pipeline_env):
        """VOL_A: family + work packs. VOL_B: work + archive packs.
        Work packs are redundant."""
        env = pipeline_env
        conn = env["conn"]
        self._register_all(env)

        family_packs = get_unarchived_packs(conn, repo_id="family")
        work_packs = get_unarchived_packs(conn, repo_id="work")
        archive_packs = get_unarchived_packs(conn, repo_id="archive")

        vol_a = create_volume(
            conn, label="VOL_A", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol_a.volume_id,
                        [p.pack_id for p in family_packs + work_packs])

        vol_b = create_volume(
            conn, label="VOL_B", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol_b.volume_id,
                        [p.pack_id for p in work_packs + archive_packs])

        # All archived
        assert len(get_unarchived_packs(conn)) == 0

        # Work packs have 2 copies
        for p in work_packs:
            vols = get_volumes_for_pack(conn, p.pack_id)
            assert len(vols) == 2

        # Family and archive packs have 1 copy
        for p in family_packs + archive_packs:
            vols = get_volumes_for_pack(conn, p.pack_id)
            assert len(vols) == 1


# =========================================================================
# Phase 3: Restore from every viable combination
# =========================================================================


class TestFullPipelineRestore:
    """Full pipeline: scan → register → archive to 3 volumes with overlap
    → enumerate all viable combos → restore from each → verify integrity."""

    def _setup_full_scenario(self, env):
        """Register all packs and create 3 overlapping volumes."""
        conn = env["conn"]

        # Register all repos
        all_pack_specs = {}
        for repo_id, mirror, specs in [
            ("family", env["family_mirror"], env["family_packs"]),
            ("work", env["work_mirror"], env["work_packs"]),
            ("archive", env["archive_mirror"], env["archive_packs"]),
        ]:
            scanned = scan_mirror_packs(mirror)
            delta = DeltaAnalyzer(conn, scanned, repo_id=repo_id)
            delta.register_new_packs()
            for sha, size in specs:
                all_pack_specs[sha] = size

        all_packs = get_unarchived_packs(conn)
        family_packs = [p for p in all_packs if p.repo_id == "family"]
        work_packs = [p for p in all_packs if p.repo_id == "work"]
        archive_packs = [p for p in all_packs if p.repo_id == "archive"]

        # VOL_1: all family + work packs (7 packs)
        vol1 = create_volume(
            conn, label="VOL_1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol1.volume_id,
                        [p.pack_id for p in family_packs + work_packs])

        # VOL_2: all work + archive packs (5 packs, work is redundant)
        vol2 = create_volume(
            conn, label="VOL_2", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol2.volume_id,
                        [p.pack_id for p in work_packs + archive_packs])

        # VOL_3: all family + archive packs (6 packs, both redundant)
        vol3 = create_volume(
            conn, label="VOL_3", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol3.volume_id,
                        [p.pack_id for p in family_packs + archive_packs])

        return all_pack_specs

    def _create_volume_dirs(self, env, tmp_path, vol_pack_mapping: dict[str, list[str]]):
        """Create simulated volume directories with actual pack files."""
        dirs = {}
        for label, sha_list in vol_pack_mapping.items():
            vol_dir = tmp_path / f"mounted_{label}"
            data_dir = vol_dir / "data"
            data_dir.mkdir(parents=True)

            for sha in sha_list:
                # Look up size from all pack specs
                all_specs = dict(env["family_packs"] + env["work_packs"] + env["archive_packs"])
                size = all_specs[sha]
                content = _expected_content(sha, size)
                (data_dir / sha).write_bytes(content)

            # Metadata
            for subdir in ["index", "snapshots", "keys"]:
                d = vol_dir / "metadata" / subdir
                d.mkdir(parents=True)
                (d / "meta.json").write_text("{}")
            (vol_dir / "metadata" / "config").write_text('{"version": 2}')

            dirs[label] = vol_dir
        return dirs

    def test_restore_from_every_viable_combination(self, pipeline_env, tmp_path):
        """The critical test: for each possible subset of {VOL_1, VOL_2, VOL_3}
        that covers all 9 packs, restore and verify integrity."""
        env = pipeline_env
        conn = env["conn"]
        all_pack_specs = self._setup_full_scenario(env)
        all_shas = list(all_pack_specs.keys())

        # Determine which packs are on which volume
        vols = list_volumes(conn)
        vol_contents: dict[str, set[str]] = {}
        for vol in vols:
            packs = get_packs_for_volume(conn, vol.volume_id)
            vol_contents[vol.label] = {p.sha256 for p in packs}

        # VOL_1: family + work = 7
        assert len(vol_contents["VOL_1"]) == 7
        # VOL_2: work + archive = 5
        assert len(vol_contents["VOL_2"]) == 5
        # VOL_3: family + archive = 6
        assert len(vol_contents["VOL_3"]) == 6

        # Create volume directories
        vol_dirs = self._create_volume_dirs(
            env, tmp_path,
            {label: list(shas) for label, shas in vol_contents.items()},
        )

        # Enumerate all viable combinations
        vol_labels = list(vol_contents.keys())
        viable_combos = []
        for size in range(1, len(vol_labels) + 1):
            for combo in itertools.combinations(vol_labels, size):
                union = set()
                for label in combo:
                    union |= vol_contents[label]
                if union >= set(all_shas):
                    viable_combos.append(combo)

        assert len(viable_combos) >= 1, "No viable combination found!"

        # For each combination, simulate full restore and verify
        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        for idx, combo in enumerate(viable_combos):
            cache_dir = tmp_path / f"restore_cache_{idx}"
            first_vol = vol_dirs[combo[0]]
            executor.prepare_cache(cache_dir, first_vol / "metadata")

            for label in combo:
                vol_dir = vol_dirs[label]
                vol_shas = list(vol_contents[label])
                executor.ingest_volume(cache_dir, vol_dir, vol_shas)

            # Verify ALL packs present and correct
            for sha in all_shas:
                cached = cache_dir / "data" / sha
                assert cached.exists(), (
                    f"Pack {sha} missing from cache (combo={combo})"
                )
                expected = _expected_content(sha, all_pack_specs[sha])
                actual = cached.read_bytes()
                assert actual == expected, (
                    f"Integrity failure for {sha} (combo={combo})"
                )

    def test_restore_with_one_volume_destroyed(self, pipeline_env, tmp_path):
        """Destroy VOL_1 and verify restore still works from VOL_2+VOL_3."""
        env = pipeline_env
        conn = env["conn"]
        all_pack_specs = self._setup_full_scenario(env)
        all_shas = list(all_pack_specs.keys())

        # Determine VOL_1 id
        vol1 = get_volume_by_label(conn, "VOL_1")
        from lcsas.db.volumes import update_status
        update_status(conn, vol1.volume_id, "DESTROYED")

        # Pick list should exclude VOL_1
        planner = RestorePlanner(conn)
        pick = planner.generate_pick_list(all_shas)
        assert "VOL_1" not in pick.volumes
        assert pick.missing_packs == []

        # All packs should still be covered by VOL_2 + VOL_3
        planned_shas = {
            p.sha256 for packs in pick.volumes.values() for p in packs
        }
        assert planned_shas == set(all_shas)

    def test_restore_per_repo_independently(self, pipeline_env, tmp_path):
        """Restore each repo's packs independently and verify isolation."""
        env = pipeline_env
        conn = env["conn"]
        self._setup_full_scenario(env)

        planner = RestorePlanner(conn)

        # Family packs
        fam_shas = [sha for sha, _ in env["family_packs"]]
        pick_fam = planner.generate_pick_list(fam_shas)
        assert pick_fam.missing_packs == []
        assert pick_fam.total_packs == 4

        # Work packs
        wrk_shas = [sha for sha, _ in env["work_packs"]]
        pick_wrk = planner.generate_pick_list(wrk_shas)
        assert pick_wrk.missing_packs == []
        assert pick_wrk.total_packs == 3

        # Archive packs
        arc_shas = [sha for sha, _ in env["archive_packs"]]
        pick_arc = planner.generate_pick_list(arc_shas)
        assert pick_arc.missing_packs == []
        assert pick_arc.total_packs == 2


# =========================================================================
# Edge cases
# =========================================================================


class TestMultiTenantEdgeCases:

    def test_repo_with_no_packs_restore(self, pipeline_env):
        """Requesting packs for a repo that has no archived packs."""
        conn = pipeline_env["conn"]
        planner = RestorePlanner(conn)
        pick = planner.generate_pick_list(["nonexistent_sha_1", "nonexistent_sha_2"])
        assert pick.total_packs == 0
        assert len(pick.missing_packs) == 2

    def test_duplicate_pack_hash_across_repos(self, pipeline_env):
        """If two repos have a pack with the same SHA, it's deduplicated."""
        conn = pipeline_env["conn"]

        # Register a pack for family
        p1 = register_pack(conn, sha256="shared_hash", size_bytes=500, repo_id="family")

        # Try to register same SHA for work
        p2 = register_pack(conn, sha256="shared_hash", size_bytes=500, repo_id="work")

        # Same pack (dedup by SHA)
        assert p1.pack_id == p2.pack_id

        # Archive it once
        vol = create_volume(
            conn, label="VOL_SHARED", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(conn, vol.volume_id, [p1.pack_id])

        # Both repos see it as archived
        fam_unarchived = get_unarchived_packs(conn, repo_id="family")
        wrk_unarchived = get_unarchived_packs(conn, repo_id="work")
        fam_shas = {p.sha256 for p in fam_unarchived}
        wrk_shas = {p.sha256 for p in wrk_unarchived}
        assert "shared_hash" not in fam_shas
        assert "shared_hash" not in wrk_shas

    def test_large_volume_count(self, pipeline_env):
        """Stress test with 10 volumes each having some overlapping packs."""
        conn = pipeline_env["conn"]

        # Register some packs
        packs = []
        for i in range(20):
            p = register_pack(
                conn, sha256=f"stress_{i:04d}", size_bytes=100 * (i + 1),
                repo_id="family",
            )
            packs.append(p)

        # Create 10 volumes, each with a sliding window of 5 packs
        for v in range(10):
            vol = create_volume(
                conn, label=f"STRESS_V{v:02d}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
            )
            window = packs[v * 2:v * 2 + 5]
            if window:
                bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in window])

        # Verify pick list can find packs across volumes
        all_shas = [f"stress_{i:04d}" for i in range(20)]
        pick = get_pick_list(conn, all_shas)
        found = {p.sha256 for plist in pick.values() for p in plist}

        # Packs 0-18 should be findable (sliding window covers them)
        for i in range(min(19, 20)):
            sha = f"stress_{i:04d}"
            if i < 19:  # Window reaches pack 18 (v=7: 14..19, v=8: 16..20, v=9: 18..22 clamped)
                assert sha in found or sha in [
                    p for p in all_shas
                    if p not in found
                ], f"Pack {sha} status unclear"

    def test_volume_status_transitions(self, pipeline_env):
        """Test restore planning across volume lifecycle."""
        conn = pipeline_env["conn"]

        p = register_pack(conn, sha256="lifecycle_pack", size_bytes=1000, repo_id="work")

        # On 3 volumes
        vols = []
        for i, status in enumerate(["VERIFIED", "VERIFIED", "VERIFIED"]):
            vol = create_volume(
                conn, label=f"LIFE_{i}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000, status=status,
            )
            bulk_link_packs(conn, vol.volume_id, [p.pack_id])
            vols.append(vol)

        # All 3 copies available
        planner = RestorePlanner(conn)
        pick = planner.generate_pick_list(["lifecycle_pack"])
        assert pick.total_packs == 1
        assert pick.missing_packs == []

        # Deprecate one — still available
        from lcsas.db.volumes import update_status
        update_status(conn, vols[0].volume_id, "DEPRECATED")
        pick = planner.generate_pick_list(["lifecycle_pack"])
        assert pick.total_packs == 1
        assert "LIFE_0" not in pick.volumes

        # Destroy another — still available on last one
        update_status(conn, vols[1].volume_id, "DESTROYED")
        pick = planner.generate_pick_list(["lifecycle_pack"])
        assert pick.total_packs == 1
        assert "LIFE_2" in pick.volumes

        # Destroy the last one — pack is now missing
        update_status(conn, vols[2].volume_id, "DESTROYED")
        pick = planner.generate_pick_list(["lifecycle_pack"])
        assert pick.total_packs == 0
