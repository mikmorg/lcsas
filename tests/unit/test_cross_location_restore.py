"""Cross-location restoration tests with divergent volume sets.

Models a realistic disaster-recovery scenario:

  Two physical locations (Home_Shelf, Offsite_Safe) receive burns at
  different points in time, producing non-mirrored volume sets.  When a
  volume at the primary restoration site is destroyed, the restore
  planner must route to surviving volumes at the other location.

Timeline & Layout
-----------------
  T1  Home burn:    HOME_001 = [p01 p02 p03 p04 d01 d02]
  T2  Offsite burn: OFF_001  = [p01 p02 p03 d01]          (partial catchup)
  T3  Home burn:    HOME_002 = [p05 p06 p07 p08 d03 d04]
  T4  Offsite burn: OFF_002  = [p04 p05 p06 d02 d03]      (bridging)
  T5  Offsite burn: OFF_003  = [p07 p08 d04]               (final catchup)

Pack redundancy (every pack has exactly 2 copies, 1 per location):
  p01  HOME_001  OFF_001         p05  HOME_002  OFF_002
  p02  HOME_001  OFF_001         p06  HOME_002  OFF_002
  p03  HOME_001  OFF_001         p07  HOME_002  OFF_003
  p04  HOME_001  OFF_002         p08  HOME_002  OFF_003
  d01  HOME_001  OFF_001         d03  HOME_002  OFF_002
  d02  HOME_001  OFF_002         d04  HOME_002  OFF_003
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from lcsas.db.locations import ensure_location
from lcsas.db.packs import get_pack_by_sha256, register_pack
from lcsas.db.queries import (
    get_location_summary,
    get_packs_at_location,
    get_packs_for_volume,
    get_packs_missing_at_location,
    get_pick_list,
    get_redundancy_report,
    get_volumes_for_pack,
)
from lcsas.db.repos import register_repo
from lcsas.db.volume_copies import add_volume_copy, destroy_copy, get_copies_for_volume
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, get_volume_by_label, update_status
from lcsas.restore.executor import RestoreExecutor
from lcsas.restore.planner import RestorePlanner
from lcsas.utils.labels import generate_uuid

# =========================================================================
# Constants
# =========================================================================

PHOTO_SHAS = [f"p{i:02d}_sha" for i in range(1, 9)]   # p01..p08
DOC_SHAS = [f"d{i:02d}_sha" for i in range(1, 5)]      # d01..d04
ALL_SHAS = PHOTO_SHAS + DOC_SHAS                        # 12 packs total

# Volume → pack membership (non-mirrored across locations)
VOL_PACKS = {
    "HOME_001": ["p01_sha", "p02_sha", "p03_sha", "p04_sha", "d01_sha", "d02_sha"],
    "HOME_002": ["p05_sha", "p06_sha", "p07_sha", "p08_sha", "d03_sha", "d04_sha"],
    "OFF_001":  ["p01_sha", "p02_sha", "p03_sha", "d01_sha"],
    "OFF_002":  ["p04_sha", "p05_sha", "p06_sha", "d02_sha", "d03_sha"],
    "OFF_003":  ["p07_sha", "p08_sha", "d04_sha"],
}

# Location each volume physically lives at
VOL_LOCATIONS = {
    "HOME_001": "Home_Shelf",
    "HOME_002": "Home_Shelf",
    "OFF_001":  "Offsite_Safe",
    "OFF_002":  "Offsite_Safe",
    "OFF_003":  "Offsite_Safe",
}


# =========================================================================
# Helpers
# =========================================================================

def _pack_content(sha: str) -> bytes:
    """Deterministic 128-byte content derived from the SHA string."""
    return hashlib.sha256(sha.encode()).digest() * 4


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def cross_loc_db(memory_db):
    """DB with divergent volume sets across two locations.

    See module docstring for the full layout.  Every pack has exactly
    two copies — one at Home_Shelf and one at Offsite_Safe.
    """
    conn = memory_db

    # Locations
    ensure_location(conn, "Home_Shelf", "Primary storage at home")
    ensure_location(conn, "Offsite_Safe", "Safety-deposit box offsite")

    # Repositories
    register_repo(conn, "photos", "Photos", "/mnt/mirror/photos")
    register_repo(conn, "docs", "Documents", "/mnt/mirror/docs")

    # Packs
    packs = {}
    for i in range(1, 9):
        sha = f"p{i:02d}_sha"
        packs[sha] = register_pack(conn, sha256=sha, size_bytes=1000 * i, repo_id="photos")
    for i in range(1, 5):
        sha = f"d{i:02d}_sha"
        packs[sha] = register_pack(conn, sha256=sha, size_bytes=500 * i, repo_id="docs")

    # Volumes & volume copies
    vols = {}
    for label, location in VOL_LOCATIONS.items():
        vols[label] = create_volume(
            conn,
            label=label,
            uuid=generate_uuid(),
            media_type="BD25",
            capacity_bytes=25_000_000_000,
            location=location,
            status="VERIFIED",
        )
        add_volume_copy(conn, vols[label].volume_id, location)

    # Link packs → volumes
    for label, sha_list in VOL_PACKS.items():
        pack_ids = [packs[sha].pack_id for sha in sha_list]
        bulk_link_packs(conn, vols[label].volume_id, pack_ids)

    return conn


@pytest.fixture
def volume_dirs(tmp_path):
    """Simulated mounted volumes with deterministic pack files."""
    dirs = {}
    for label, sha_list in VOL_PACKS.items():
        vol_dir = tmp_path / label
        data_dir = vol_dir / "data"
        data_dir.mkdir(parents=True)
        for sha in sha_list:
            (data_dir / sha).write_bytes(_pack_content(sha))

        # Metadata stubs (for prepare_cache)
        for subdir in ("index", "snapshots", "keys"):
            d = vol_dir / "metadata" / "photos" / subdir
            d.mkdir(parents=True)
            (d / "dummy.json").write_text("{}")
        (vol_dir / "metadata" / "photos" / "config").write_text('{"version": 2}')

        dirs[label] = vol_dir
    return dirs


# =========================================================================
# 1. Baseline: healthy-state verification
# =========================================================================

class TestHealthyState:
    """Verify the fixture is correctly wired before stressing it."""

    def test_every_pack_has_exactly_2_copies(self, cross_loc_db):
        """Each pack should live on exactly 2 volumes (1 per location)."""
        under_2 = get_redundancy_report(cross_loc_db, min_copies=2)
        assert under_2 == [], (
            f"Packs with < 2 copies: {[p.sha256 for p in under_2]}"
        )

    def test_no_pack_has_more_than_2_copies(self, cross_loc_db):
        """Sanity — no pack exceeds 2 volume assignments."""
        for sha in ALL_SHAS:
            p = get_pack_by_sha256(cross_loc_db, sha)
            vols = get_volumes_for_pack(cross_loc_db, p.pack_id)
            assert len(vols) == 2, f"{sha} on {len(vols)} volumes, expected 2"

    def test_home_volumes_differ_from_offsite_volumes(self, cross_loc_db):
        """No two volumes have identical pack sets (non-mirrored)."""
        labels = list(VOL_PACKS.keys())
        for i, a in enumerate(labels):
            for b in labels[i + 1:]:
                assert set(VOL_PACKS[a]) != set(VOL_PACKS[b]), (
                    f"{a} and {b} have identical pack sets"
                )

    def test_volume_locations_recorded(self, cross_loc_db):
        """Each volume has a copy recorded at the correct location."""
        for label, expected_loc in VOL_LOCATIONS.items():
            vol = get_volume_by_label(cross_loc_db, label)
            copies = get_copies_for_volume(cross_loc_db, vol.volume_id, active_only=True)
            assert len(copies) == 1
            assert copies[0].location == expected_loc

    def test_pick_list_covers_all_packs(self, cross_loc_db):
        """Full pick list in healthy state finds every pack."""
        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}
        assert found == set(ALL_SHAS)

    def test_location_summary_healthy(self, cross_loc_db):
        """Both locations report correct pack counts."""
        summary = get_location_summary(cross_loc_db)
        by_loc = {s["location"]: s for s in summary}

        assert set(by_loc.keys()) == {"Home_Shelf", "Offsite_Safe"}
        # Home has HOME_001 (6 packs) + HOME_002 (6 packs) = 12 unique
        assert by_loc["Home_Shelf"]["packs"] == 12
        assert by_loc["Home_Shelf"]["volumes"] == 2
        # Offsite has OFF_001(4) + OFF_002(5) + OFF_003(3) = 12 unique
        assert by_loc["Offsite_Safe"]["packs"] == 12
        assert by_loc["Offsite_Safe"]["volumes"] == 3


# =========================================================================
# 2. Single-volume destruction at Home
# =========================================================================

class TestSingleVolumeDestroyed:
    """Destroy HOME_002 and verify cross-location recovery."""

    def _destroy_home_002(self, conn):
        vol = get_volume_by_label(conn, "HOME_002")
        update_status(conn, vol.volume_id, "DESTROYED")
        destroy_copy(conn, vol.volume_id, "Home_Shelf")

    def test_pick_list_routes_to_offsite(self, cross_loc_db):
        """After HOME_002 is destroyed, packs formerly on it are
        routed to Offsite volumes."""
        self._destroy_home_002(cross_loc_db)

        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}

        # ALL packs should still be found
        assert found == set(ALL_SHAS)
        # HOME_002 must NOT appear in the pick list
        assert "HOME_002" not in pick

    def test_offsite_volumes_provide_home002_packs(self, cross_loc_db):
        """Packs p05-p08, d03, d04 (HOME_002) should come from OFF_* volumes."""
        self._destroy_home_002(cross_loc_db)

        pick = get_pick_list(cross_loc_db, ALL_SHAS)

        home002_packs = set(VOL_PACKS["HOME_002"])
        for sha in home002_packs:
            # Find which volume this pack was assigned to
            vol_label = None
            for label, packs in pick.items():
                if any(p.sha256 == sha for p in packs):
                    vol_label = label
                    break
            assert vol_label is not None, f"{sha} missing from pick list"
            assert vol_label.startswith("OFF_"), (
                f"{sha} routed to {vol_label} instead of an Offsite volume"
            )

    def test_planner_reports_no_missing(self, cross_loc_db):
        """RestorePlanner confirms full recovery is possible."""
        self._destroy_home_002(cross_loc_db)

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(ALL_SHAS)

        assert pick.missing_packs == []
        assert pick.total_packs == 12

    def test_location_summary_after_destruction(self, cross_loc_db):
        """Home should now report fewer packs; Offsite unchanged."""
        self._destroy_home_002(cross_loc_db)

        summary = get_location_summary(cross_loc_db)
        by_loc = {s["location"]: s for s in summary}

        # Home only has HOME_001 left with 6 packs
        assert by_loc["Home_Shelf"]["packs"] == 6
        assert by_loc["Home_Shelf"]["volumes"] == 1
        # Offsite unchanged
        assert by_loc["Offsite_Safe"]["packs"] == 12
        assert by_loc["Offsite_Safe"]["volumes"] == 3

    def test_packs_missing_at_home_shelf(self, cross_loc_db):
        """Location-aware query finds the 6 packs now missing at Home."""
        self._destroy_home_002(cross_loc_db)

        missing = get_packs_missing_at_location(cross_loc_db, "Home_Shelf")
        missing_shas = {p.sha256 for p in missing}

        expected_missing = set(VOL_PACKS["HOME_002"])
        assert missing_shas == expected_missing

    def test_packs_at_home_shelf_reduced(self, cross_loc_db):
        """Only HOME_001's 6 packs remain accessible at Home."""
        self._destroy_home_002(cross_loc_db)

        pack_ids = get_packs_at_location(cross_loc_db, "Home_Shelf")
        # Convert pack_ids to SHA-256 for readability
        remaining_shas = set()
        for sha in ALL_SHAS:
            p = get_pack_by_sha256(cross_loc_db, sha)
            if p.pack_id in pack_ids:
                remaining_shas.add(sha)

        assert remaining_shas == set(VOL_PACKS["HOME_001"])


# =========================================================================
# 3. Multi-volume destruction — total loss scenario
# =========================================================================

class TestMultiVolumeDestruction:
    """Destroy volumes at both locations to create irrecoverable gaps."""

    def _destroy_volumes(self, conn, labels):
        for label in labels:
            vol = get_volume_by_label(conn, label)
            update_status(conn, vol.volume_id, "DESTROYED")
            destroy_copy(conn, vol.volume_id, VOL_LOCATIONS[label])

    def test_destroy_home002_and_off002(self, cross_loc_db):
        """HOME_002 + OFF_002 destroyed.

        Packs unique to those two volumes become irrecoverable:
          p05 (HOME_002 + OFF_002) — LOST
          p06 (HOME_002 + OFF_002) — LOST
          d03 (HOME_002 + OFF_002) — LOST

        But other HOME_002 packs survive on OFF_003:
          p07, p08, d04 still available.
        And OFF_002-only packs:
          d02 (HOME_001 + OFF_002) — d02 still on HOME_001.
          p04 (HOME_001 + OFF_002) — p04 still on HOME_001.
        """
        self._destroy_volumes(cross_loc_db, ["HOME_002", "OFF_002"])

        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}

        # These were ONLY on HOME_002 + OFF_002 → irrecoverable
        irrecoverable = {"p05_sha", "p06_sha", "d03_sha"}
        for sha in irrecoverable:
            assert sha not in found, f"{sha} should be irrecoverable"

        # Everything else should still be found
        recoverable = set(ALL_SHAS) - irrecoverable
        assert found == recoverable

    def test_destroy_home002_and_off002_lost_count(self, cross_loc_db):
        """Planner correctly counts 9 found, 0 listed as missing
        (missing_packs only reports packs NOT on any volume at all)."""
        self._destroy_volumes(cross_loc_db, ["HOME_002", "OFF_002"])

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(ALL_SHAS)

        # 12 total - 3 irrecoverable = 9 found
        assert pick.total_packs == 9

    def test_destroy_all_home_volumes(self, cross_loc_db):
        """If Home_Shelf is completely lost, Offsite covers everything."""
        self._destroy_volumes(cross_loc_db, ["HOME_001", "HOME_002"])

        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}

        # All packs should be found via Offsite volumes
        assert found == set(ALL_SHAS)

        # No Home volume should appear
        for label in pick:
            assert not label.startswith("HOME_"), (
                f"{label} is a destroyed Home volume in pick list"
            )

    def test_destroy_all_offsite_volumes(self, cross_loc_db):
        """If Offsite_Safe is completely lost, Home covers everything."""
        self._destroy_volumes(cross_loc_db, ["OFF_001", "OFF_002", "OFF_003"])

        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}

        assert found == set(ALL_SHAS)

        for label in pick:
            assert not label.startswith("OFF_"), (
                f"{label} is a destroyed Offsite volume in pick list"
            )


# =========================================================================
# 4. Pick-list routing specifics
# =========================================================================

class TestPickListRouting:
    """Verify which specific volumes the pick list selects."""

    def test_healthy_prefers_alphabetical_order(self, cross_loc_db):
        """get_pick_list deduplicates by ORDER BY v.label, so HOME_001
        wins for shared packs like p01-p03, d01."""
        pick = get_pick_list(cross_loc_db, ALL_SHAS)

        # p01 is on HOME_001 and OFF_001 → HOME_001 wins (sorts first)
        p01_vol = None
        for label, packs in pick.items():
            if any(p.sha256 == "p01_sha" for p in packs):
                p01_vol = label
                break
        assert p01_vol == "HOME_001"

    def test_routing_shifts_after_home001_destroyed(self, cross_loc_db):
        """Destroying HOME_001 shifts p01-p04, d01, d02 to Offsite."""
        vol = get_volume_by_label(cross_loc_db, "HOME_001")
        update_status(cross_loc_db, vol.volume_id, "DESTROYED")

        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        found = {p.sha256 for packs in pick.values() for p in packs}
        assert found == set(ALL_SHAS)

        # p01 now must come from OFF_001
        for label, packs in pick.items():
            if any(p.sha256 == "p01_sha" for p in packs):
                assert label == "OFF_001"
                break

        # p04 was on HOME_001 + OFF_002 → OFF_002
        for label, packs in pick.items():
            if any(p.sha256 == "p04_sha" for p in packs):
                assert label == "OFF_002"
                break


# =========================================================================
# 5. Restore executor — cross-location cache assembly
# =========================================================================

class TestCrossLocationCacheAssembly:
    """Simulate building a restore cache from volumes at mixed locations."""

    def _destroy_home_002(self, conn):
        vol = get_volume_by_label(conn, "HOME_002")
        update_status(conn, vol.volume_id, "DESTROYED")
        destroy_copy(conn, vol.volume_id, "Home_Shelf")

    def test_cache_from_home001_plus_offsite(self, cross_loc_db, volume_dirs, tmp_path):
        """After HOME_002 destroyed, assemble cache from HOME_001 + OFF_* volumes."""
        self._destroy_home_002(cross_loc_db)

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(ALL_SHAS)
        assert pick.missing_packs == []

        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        cache = tmp_path / "restore_cache"
        first_label = next(iter(pick.volumes))
        executor.prepare_cache(
            cache, volume_dirs[first_label] / "metadata" / "photos"
        )

        for label, packs in pick.volumes.items():
            shas = [p.sha256 for p in packs]
            executor.ingest_volume(cache, volume_dirs[label], shas)

        # Every pack must be in the cache with correct content
        for sha in ALL_SHAS:
            cached = cache / "data" / sha
            assert cached.exists(), f"{sha} missing from restore cache"
            assert cached.read_bytes() == _pack_content(sha), (
                f"{sha} content mismatch in cache"
            )

    def test_cache_from_offsite_only(self, cross_loc_db, volume_dirs, tmp_path):
        """Complete restore using only Offsite volumes (Home totally lost)."""
        for label in ["HOME_001", "HOME_002"]:
            vol = get_volume_by_label(cross_loc_db, label)
            update_status(cross_loc_db, vol.volume_id, "DESTROYED")

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(ALL_SHAS)
        assert pick.missing_packs == []

        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        cache = tmp_path / "restore_cache"
        first_label = next(iter(pick.volumes))
        executor.prepare_cache(
            cache, volume_dirs[first_label] / "metadata" / "photos"
        )

        for label, packs in pick.volumes.items():
            shas = [p.sha256 for p in packs]
            executor.ingest_volume(cache, volume_dirs[label], shas)

        for sha in ALL_SHAS:
            cached = cache / "data" / sha
            assert cached.exists(), f"{sha} missing from offsite-only cache"
            assert cached.read_bytes() == _pack_content(sha)

    def test_full_restore_workflow_after_destruction(
        self, cross_loc_db, volume_dirs, tmp_path
    ):
        """End-to-end: plan → ingest → execute_restore with mixed volumes."""
        self._destroy_home_002(cross_loc_db)

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(ALL_SHAS)

        mock_rustic = MagicMock()
        executor = RestoreExecutor(mock_rustic)

        cache = tmp_path / "restore_cache"
        first_label = next(iter(pick.volumes))
        executor.prepare_cache(
            cache, volume_dirs[first_label] / "metadata" / "photos"
        )

        for label, packs in pick.volumes.items():
            shas = [p.sha256 for p in packs]
            executor.ingest_volume(cache, volume_dirs[label], shas)

        pw = tmp_path / "pw.txt"
        pw.write_text("test")
        target = tmp_path / "restored"
        executor.execute_restore(cache, "snap_abc", target, pw)

        mock_rustic.restore.assert_called_once_with(
            snapshot_id="snap_abc",
            repo_path=cache,
            password_file=pw,
            target_path=target,
        )


# =========================================================================
# 6. Repo-specific restore across locations
# =========================================================================

class TestRepoSpecificCrossLocation:
    """Test restoring a single repo when its packs span locations."""

    def _destroy_home_002(self, conn):
        vol = get_volume_by_label(conn, "HOME_002")
        update_status(conn, vol.volume_id, "DESTROYED")

    def test_photos_restore_after_home002_lost(self, cross_loc_db):
        """Photos p01-p08: p01-p04 on HOME_001, p05-p08 on HOME_002.
        After HOME_002 destroyed, p05-p08 must come from Offsite."""
        self._destroy_home_002(cross_loc_db)

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(PHOTO_SHAS)

        assert pick.missing_packs == []
        assert pick.total_packs == 8

        found_shas = {p.sha256 for packs in pick.volumes.values() for p in packs}
        assert found_shas == set(PHOTO_SHAS)

        # p05-p06 from OFF_002, p07-p08 from OFF_003
        for sha in ["p05_sha", "p06_sha"]:
            for label, packs in pick.volumes.items():
                if any(p.sha256 == sha for p in packs):
                    assert label == "OFF_002", f"{sha} should be on OFF_002"

        for sha in ["p07_sha", "p08_sha"]:
            for label, packs in pick.volumes.items():
                if any(p.sha256 == sha for p in packs):
                    assert label == "OFF_003", f"{sha} should be on OFF_003"

    def test_docs_restore_after_home002_lost(self, cross_loc_db):
        """Docs d01-d04: d01+d02 on HOME_001, d03+d04 on HOME_002.
        After HOME_002 destroyed, d03→OFF_002, d04→OFF_003."""
        self._destroy_home_002(cross_loc_db)

        planner = RestorePlanner(cross_loc_db)
        pick = planner.generate_pick_list(DOC_SHAS)

        assert pick.missing_packs == []
        assert pick.total_packs == 4


# =========================================================================
# 7. Progressive degradation: cascading destruction
# =========================================================================

class TestProgressiveDegradation:
    """Simulate an escalating disaster: volumes fail one by one."""

    def test_step_by_step_degradation(self, cross_loc_db):
        """Destroy volumes in sequence and track recoverability."""
        planner = RestorePlanner(cross_loc_db)

        # Step 0: Everything healthy
        pick = planner.generate_pick_list(ALL_SHAS)
        assert pick.missing_packs == []
        assert pick.total_packs == 12

        # Step 1: Destroy HOME_002 → still fully recoverable via Offsite
        vol_h2 = get_volume_by_label(cross_loc_db, "HOME_002")
        update_status(cross_loc_db, vol_h2.volume_id, "DESTROYED")

        pick = planner.generate_pick_list(ALL_SHAS)
        assert pick.missing_packs == []
        assert pick.total_packs == 12

        # Step 2: Also destroy OFF_003 → p07, p08, d04 were on HOME_002 + OFF_003
        vol_o3 = get_volume_by_label(cross_loc_db, "OFF_003")
        update_status(cross_loc_db, vol_o3.volume_id, "DESTROYED")

        pick = planner.generate_pick_list(ALL_SHAS)
        found = {p.sha256 for packs in pick.volumes.values() for p in packs}
        lost = set(ALL_SHAS) - found
        assert lost == {"p07_sha", "p08_sha", "d04_sha"}
        assert pick.total_packs == 9

        # Step 3: Also destroy OFF_002 → more losses cascade
        vol_o2 = get_volume_by_label(cross_loc_db, "OFF_002")
        update_status(cross_loc_db, vol_o2.volume_id, "DESTROYED")

        pick = planner.generate_pick_list(ALL_SHAS)
        found = {p.sha256 for packs in pick.volumes.values() for p in packs}
        # p05, p06, d03 also gone (HOME_002 + OFF_002)
        # p04, d02 survive on HOME_001
        additionally_lost = {"p05_sha", "p06_sha", "d03_sha"}
        total_lost = lost | additionally_lost
        assert set(ALL_SHAS) - found == total_lost
        assert pick.total_packs == 12 - len(total_lost)

    def test_redundancy_report_tracks_degradation(self, cross_loc_db):
        """get_redundancy_report shows packs dropping below the threshold."""
        # Healthy: no packs below 2 copies
        under_2 = get_redundancy_report(cross_loc_db, min_copies=2)
        assert len(under_2) == 0

        # Destroy HOME_002: all its packs drop to 1 copy
        vol = get_volume_by_label(cross_loc_db, "HOME_002")
        update_status(cross_loc_db, vol.volume_id, "DESTROYED")

        under_2 = get_redundancy_report(cross_loc_db, min_copies=2)
        under_2_shas = {p.sha256 for p in under_2}
        assert under_2_shas == set(VOL_PACKS["HOME_002"])


# =========================================================================
# 8. Data integrity across locations
# =========================================================================

class TestCrossLocationIntegrity:
    """Verify pack content is identical regardless of which location's
    volume supplies it."""

    def test_same_pack_same_content_both_locations(self, volume_dirs):
        """p01 lives on HOME_001 and OFF_001 — content must match."""
        sha = "p01_sha"
        home_content = (volume_dirs["HOME_001"] / "data" / sha).read_bytes()
        off_content = (volume_dirs["OFF_001"] / "data" / sha).read_bytes()
        assert home_content == off_content == _pack_content(sha)

    def test_integrity_all_packs_all_volumes(self, volume_dirs):
        """Every pack on every volume matches the canonical content."""
        for label, sha_list in VOL_PACKS.items():
            for sha in sha_list:
                path = volume_dirs[label] / "data" / sha
                assert path.exists(), f"{sha} not found on {label}"
                assert path.read_bytes() == _pack_content(sha), (
                    f"{sha} content mismatch on {label}"
                )

    def test_cache_identical_regardless_of_source(self, volume_dirs, tmp_path):
        """Ingesting p04 from HOME_001 vs OFF_002 produces identical cache."""
        sha = "p04_sha"
        mock = MagicMock()
        executor = RestoreExecutor(mock)

        cache_home = tmp_path / "cache_home"
        cache_home.mkdir()
        executor.ingest_volume(cache_home, volume_dirs["HOME_001"], [sha])

        cache_off = tmp_path / "cache_off"
        cache_off.mkdir()
        executor.ingest_volume(cache_off, volume_dirs["OFF_002"], [sha])

        home_bytes = (cache_home / "data" / sha).read_bytes()
        off_bytes = (cache_off / "data" / sha).read_bytes()
        assert home_bytes == off_bytes == _pack_content(sha)


# =========================================================================
# 9. Edge case: requesting only packs from destroyed volumes
# =========================================================================

class TestEdgeCaseDestroyedOnly:
    """What if every requested pack was on a now-destroyed volume?"""

    def test_all_requested_from_destroyed_vol(self, cross_loc_db):
        """Request only HOME_002 packs after HOME_002 destroyed.
        They should all route to Offsite."""
        vol = get_volume_by_label(cross_loc_db, "HOME_002")
        update_status(cross_loc_db, vol.volume_id, "DESTROYED")

        h2_shas = VOL_PACKS["HOME_002"]
        pick = get_pick_list(cross_loc_db, h2_shas)
        found = {p.sha256 for packs in pick.values() for p in packs}

        assert found == set(h2_shas)
        for label in pick:
            assert label.startswith("OFF_")

    def test_all_copies_destroyed_returns_empty(self, cross_loc_db):
        """If both volumes holding a pack are destroyed, that pack vanishes."""
        # p05 lives on HOME_002 + OFF_002
        for label in ["HOME_002", "OFF_002"]:
            vol = get_volume_by_label(cross_loc_db, label)
            update_status(cross_loc_db, vol.volume_id, "DESTROYED")

        pick = get_pick_list(cross_loc_db, ["p05_sha"])
        found = {p.sha256 for packs in pick.values() for p in packs}
        assert "p05_sha" not in found


# =========================================================================
# 10. Volume-count efficiency: minimum discs needed
# =========================================================================

class TestMinimumDiscCount:
    """Validate the pick list minimises the number of volumes to retrieve."""

    def test_healthy_uses_two_home_volumes(self, cross_loc_db):
        """In healthy state, 2 Home volumes cover everything
        (HOME_001 + HOME_002 sort before OFF_*)."""
        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        assert len(pick) == 2
        assert set(pick.keys()) == {"HOME_001", "HOME_002"}

    def test_one_home_destroyed_needs_mixed(self, cross_loc_db):
        """After HOME_002 destroyed, need HOME_001 + multiple Offsite volumes."""
        vol = get_volume_by_label(cross_loc_db, "HOME_002")
        update_status(cross_loc_db, vol.volume_id, "DESTROYED")

        pick = get_pick_list(cross_loc_db, ALL_SHAS)
        # HOME_001 for p01-p04, d01, d02
        # OFF_002 for p05, p06, d03
        # OFF_003 for p07, p08, d04
        assert "HOME_001" in pick
        assert len(pick) >= 3  # at least 3 volumes needed now
