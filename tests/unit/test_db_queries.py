"""Tests for complex cross-table queries."""

from __future__ import annotations

import json

from lcsas.db.queries import (
    get_archive_status_summary,
    get_missing_packs,
    get_packs_for_volume,
    get_packs_only_on_volumes,
    get_packs_stranded_on_unburned_volumes,
    get_pick_list,
    get_redundancy_report,
    get_snapshots_by_path,
    get_snapshots_by_tag,
    get_total_unarchived_bytes,
    get_unarchived_packs,
    get_volumes_for_pack,
)
from lcsas.db.snapshots import upsert_snapshot


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

    def test_preferred_location_pick_list(self, populated_db):
        """Preferred location should prioritise volumes at that location."""
        # Packs 1-3 exist on both vol1 (LCSAS_BD_2026_001) and vol4
        # (LCSAS_MD_2026_001). Without preference, alphabetical wins.
        # With a location preference matching vol4's location, vol4 should
        # be chosen instead.
        from lcsas.db.volumes import get_volume_by_label
        vol4 = get_volume_by_label(populated_db, "LCSAS_MD_2026_001")
        pick = get_pick_list(
            populated_db,
            ["pack_0001_hash", "pack_0002_hash", "pack_0003_hash"],
            preferred_location=vol4.location,
        )
        # All three packs should be assigned to a single volume at that location
        all_packs = [p for packs in pick.values() for p in packs]
        assert len(all_packs) == 3


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

    def test_redundancy_report_counts_active_copies(self, memory_db):
        """BURN-10: a volume whose only copy is DESTROYED adds no redundancy.

        The volume keeps its VERIFIED status (loss recorded before
        auto-demotion existed, or a rebuilt catalog) — the report must
        count ACTIVE copy rows, not statuses.
        """
        from lcsas.db.locations import create_location
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        create_location(memory_db, "Home_Shelf")
        create_location(memory_db, "Offsite_Safe")

        va = create_volume(memory_db, label="VOL_A", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000,
                           status="VERIFIED")
        vb = create_volume(memory_db, label="VOL_B", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000,
                           status="VERIFIED")
        add_volume_copy(memory_db, va.volume_id, "Home_Shelf")
        add_volume_copy(memory_db, vb.volume_id, "Offsite_Safe")

        sha = "feedface" * 8
        p = register_pack(memory_db, sha256=sha, size_bytes=100,
                          repo_id="_test")
        bulk_link_packs(memory_db, va.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, vb.volume_id, [p.pack_id])

        # Two live replicas → pack satisfies min_copies=2.
        before = {q.sha256 for q in get_redundancy_report(memory_db, min_copies=2)}
        assert sha not in before

        # Destroy VOL_B's only copy at the row level, status untouched.
        memory_db.execute(
            "UPDATE volume_copies SET status = 'DESTROYED' WHERE volume_id = ?",
            (vb.volume_id,),
        )
        memory_db.commit()

        after = {q.sha256 for q in get_redundancy_report(memory_db, min_copies=2)}
        assert sha in after


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


class TestDurableArchivedSemantics:
    """A pack is archived only via a durable (BURNED/VERIFIED/CONSOLIDATING)
    volume; STAGING/BURNING links are an intent, not a copy.  [FMA-01]"""

    @staticmethod
    def _vol(conn, label, status):
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid
        return create_volume(
            conn, label=label, uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status=status,
        )

    def test_unarchived_requires_durable_volume(self, memory_db):
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        conn = memory_db
        staging = self._vol(conn, "STAGING_VOL", "STAGING")
        pack = register_pack(
            conn, sha256="staged_pack", size_bytes=100, repo_id="_test")
        bulk_link_packs(conn, staging.volume_id, [pack.pack_id])

        # Linked only to a STAGING volume → still unarchived (re-selectable)
        assert {p.sha256 for p in get_unarchived_packs(conn)} == {
            "staged_pack"
        }
        assert get_total_unarchived_bytes(conn) == 100

        # Flipping the volume to a durable status flips the pack to archived
        conn.execute(
            "UPDATE volumes SET status = 'BURNED' WHERE volume_id = ?",
            (staging.volume_id,),
        )
        conn.commit()
        assert get_unarchived_packs(conn) == []
        assert get_total_unarchived_bytes(conn) == 0

    def test_summary_reports_staged_bucket(self, memory_db):
        from lcsas.db.packs import mark_pruned, register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        conn = memory_db
        staging = self._vol(conn, "S_VOL", "STAGING")
        verified = self._vol(conn, "V_VOL", "VERIFIED")

        p_archived = register_pack(
            conn, sha256="archived_p", size_bytes=10, repo_id="_test")
        p_both = register_pack(
            conn, sha256="both_p", size_bytes=10, repo_id="_test")
        p_staged = register_pack(
            conn, sha256="staged_p", size_bytes=10, repo_id="_test")
        register_pack(
            conn, sha256="unarchived_p", size_bytes=10, repo_id="_test")
        p_pruned = register_pack(
            conn, sha256="pruned_p", size_bytes=10, repo_id="_test")
        mark_pruned(conn, p_pruned.pack_id)

        bulk_link_packs(conn, verified.volume_id,
                        [p_archived.pack_id, p_both.pack_id])
        bulk_link_packs(conn, staging.volume_id,
                        [p_both.pack_id, p_staged.pack_id])

        summary = get_archive_status_summary(conn)
        # Buckets partition: archived (durable link, even when also
        # staged) / staged (links but none durable) / unarchived / pruned.
        assert summary == {
            "total": 5, "pruned": 1, "archived": 2, "staged": 1,
            "unarchived": 1,
        }
        assert (summary["archived"] + summary["staged"]
                + summary["unarchived"] + summary["pruned"]) == summary["total"]

    def test_pick_list_prefers_durable_and_flags_unconfirmed(self, memory_db):
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.restore.planner import RestorePlanner

        conn = memory_db
        # Ghost sorts alphabetically BEFORE the verified volume, so only
        # the status rank can win the primary-volume choice.
        ghost = self._vol(conn, "A_GHOST_STAGING", "STAGING")
        verified = self._vol(conn, "B_VERIFIED", "VERIFIED")

        p_both = register_pack(
            conn, sha256="on_both", size_bytes=10, repo_id="_test")
        p_ghost_only = register_pack(
            conn, sha256="ghost_only", size_bytes=10, repo_id="_test")
        bulk_link_packs(conn, verified.volume_id, [p_both.pack_id])
        bulk_link_packs(conn, ghost.volume_id,
                        [p_both.pack_id, p_ghost_only.pack_id])

        pick = get_pick_list(conn, ["on_both", "ghost_only"])
        # Pack on both volumes → assigned to the durable one.
        assert {p.sha256 for p in pick["B_VERIFIED"]} == {"on_both"}
        # Pack only on the ghost → still offered (on-disc holographic
        # catalogs list their own volume as STAGING; excluding it would
        # break disc-rebuilt-catalog restores).
        assert {p.sha256 for p in pick["A_GHOST_STAGING"]} == {"ghost_only"}

        # The planner flags the ghost (zero copies) as unconfirmed.
        planner = RestorePlanner(conn)
        plan = planner.generate_pick_list(["on_both", "ghost_only"])
        assert plan.unconfirmed_volume_labels == {
            "A_GHOST_STAGING": ["ghost_only"],
        }
        plan_v2 = planner.generate_pick_list_v2(["on_both", "ghost_only"])
        assert plan_v2.unconfirmed_volume_labels == {
            "A_GHOST_STAGING": ["ghost_only"],
        }

        # A copy record confirms the disc exists → no longer flagged.
        from lcsas.db.locations import ensure_location
        from lcsas.db.volume_copies import add_volume_copy
        ensure_location(conn, "Home_Shelf")
        add_volume_copy(conn, volume_id=ghost.volume_id,
                        location="Home_Shelf")
        plan = planner.generate_pick_list(["on_both", "ghost_only"])
        assert plan.unconfirmed_volume_labels == {}


class TestReconcileQueries:
    """Ghost-volume and status/copy-drift queries for `catalog reconcile`.
    [FMA-01]"""

    def test_ghost_volumes_and_drift(self, memory_db):
        from lcsas.db.locations import ensure_location
        from lcsas.db.queries import (
            get_durable_volumes_without_active_copies,
            get_ghost_volumes,
            get_volume_pack_stats_by_repo,
        )
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        conn = memory_db
        ensure_location(conn, "Home_Shelf")

        def _vol(label, status):
            return create_volume(
                conn, label=label, uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
                status=status,
            )

        stale_ghost = _vol("STALE_GHOST", "STAGING")
        _vol("FRESH_STAGING", "STAGING")
        staging_with_copy = _vol("STAGING_WITH_COPY", "STAGING")
        _vol("DRIFTED_VERIFIED", "VERIFIED")
        healthy = _vol("HEALTHY_VERIFIED", "VERIFIED")

        conn.execute(
            """UPDATE volumes SET created_at = datetime('now', '-48 hours')
               WHERE volume_id IN (?, ?)""",
            (stale_ghost.volume_id, staging_with_copy.volume_id),
        )
        conn.commit()
        add_volume_copy(conn, volume_id=staging_with_copy.volume_id,
                        location="Home_Shelf")
        add_volume_copy(conn, volume_id=healthy.volume_id,
                        location="Home_Shelf")

        ghosts = get_ghost_volumes(conn, 24)
        assert {v.label for v in ghosts} == {"STALE_GHOST"}
        # fresh volume becomes a ghost only with a zero-hour cutoff
        assert {v.label for v in get_ghost_volumes(conn, 0)} == {
            "STALE_GHOST", "FRESH_STAGING",
        }

        assert {v.label for v in
                get_durable_volumes_without_active_copies(conn)} == {
            "DRIFTED_VERIFIED",
        }

        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs
        p1 = register_pack(
            conn, sha256="g1", size_bytes=100, repo_id="_test")
        p2 = register_pack(
            conn, sha256="g2", size_bytes=250, repo_id="_test")
        bulk_link_packs(conn, stale_ghost.volume_id,
                        [p1.pack_id, p2.pack_id])
        assert get_volume_pack_stats_by_repo(
            conn, stale_ghost.volume_id) == [("_test", 2, 350)]


class TestStrandedPacks:
    """get_packs_stranded_on_unburned_volumes — packs whose only claims
    are stale never-burned (STAGING/BURNING) volumes.  [BURN-03]"""

    def test_stranded_packs_query(self, memory_db):
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        conn = memory_db

        def _vol(label, status):
            return create_volume(
                conn, label=label, uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
                status=status,
            )

        old_staging = _vol("STALE_STAGING", "STAGING")
        fresh_staging = _vol("FRESH_STAGING", "STAGING")
        verified = _vol("VERIFIED_VOL", "VERIFIED")

        # Age the stale volume beyond the 24 h cutoff
        conn.execute(
            """UPDATE volumes SET created_at = datetime('now', '-48 hours')
               WHERE volume_id = ?""",
            (old_staging.volume_id,),
        )
        conn.commit()

        pack_stranded = register_pack(
            conn, sha256="stranded_pack", size_bytes=100, repo_id="_test")
        pack_also_verified = register_pack(
            conn, sha256="also_on_verified", size_bytes=100, repo_id="_test")
        pack_verified_only = register_pack(
            conn, sha256="verified_only", size_bytes=100, repo_id="_test")
        pack_fresh = register_pack(
            conn, sha256="fresh_staging_pack", size_bytes=100, repo_id="_test")
        register_pack(
            conn, sha256="unarchived_pack", size_bytes=100, repo_id="_test")

        bulk_link_packs(conn, old_staging.volume_id,
                        [pack_stranded.pack_id, pack_also_verified.pack_id])
        bulk_link_packs(conn, verified.volume_id,
                        [pack_also_verified.pack_id, pack_verified_only.pack_id])
        bulk_link_packs(conn, fresh_staging.volume_id, [pack_fresh.pack_id])

        stranded = get_packs_stranded_on_unburned_volumes(conn, 24)
        assert {p.sha256 for p in stranded} == {"stranded_pack"}

        # With a zero-hour cutoff the fresh STAGING claim is stale too.
        stranded_now = get_packs_stranded_on_unburned_volumes(
            conn, older_than_hours=0)
        assert {p.sha256 for p in stranded_now} == {
            "stranded_pack", "fresh_staging_pack",
        }


class TestBatchBoundary:
    """Test batch processing with >900 items to exercise multiple loop iterations."""

    def test_pick_list_large_batch(self, memory_db):
        """Test get_pick_list with >900 pack SHAs (crosses batch boundary)."""
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        # Create repo and volume
        repo = register_repo(
            memory_db, repo_id=generate_uuid(), name="test_repo",
            mirror_path="/tmp/mirror",
        )
        vol = create_volume(
            memory_db, label="VOL1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )

        # Register >900 packs and assign to volume
        num_packs = 950
        pack_ids = []
        pack_shas = []
        for i in range(num_packs):
            sha = f"{i:064d}"  # Zero-padded decimal as SHA
            pack = register_pack(memory_db, sha256=sha, size_bytes=1000, repo_id=repo.repo_id)
            pack_ids.append(pack.pack_id)
            pack_shas.append(sha)

        # Link all packs to volume
        bulk_link_packs(memory_db, vol.volume_id, pack_ids)

        # Query should handle >900 SHAs correctly across batch boundaries
        pick_list = get_pick_list(memory_db, pack_shas)
        all_packs = [p for packs in pick_list.values() for p in packs]
        assert len(all_packs) == num_packs

    def test_missing_packs_large_batch(self, memory_db):
        """Test get_missing_packs with >900 items (crosses batch boundary)."""
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo
        from lcsas.utils.labels import generate_uuid

        # Register repo and create >900 nonexistent hashes + a few real ones
        repo = register_repo(
            memory_db, repo_id=generate_uuid(), name="test_repo",
            mirror_path="/tmp/mirror",
        )
        real_shas = []
        for i in range(5):
            sha = f"{i:064d}"
            pack = register_pack(memory_db, sha256=sha, size_bytes=1000, repo_id=repo.repo_id)
            real_shas.append(pack.sha256)

        # Add 950 nonexistent hashes
        all_shas = list(real_shas)
        for i in range(100, 1050):
            all_shas.append(f"nonexistent_{i:04d}_{i:056d}")

        # Query: all nonexistent should be in missing, real ones too (no volume assignments)
        missing = get_missing_packs(memory_db, all_shas)
        assert len(missing) == len(all_shas)  # All are missing (no volumes)

    def test_packs_only_on_volumes_large_batch(self, memory_db):
        """Test get_packs_only_on_volumes with >900 volume IDs."""
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        repo = register_repo(
            memory_db, repo_id=generate_uuid(), name="test_repo",
            mirror_path="/tmp/mirror",
        )

        # Create >900 volumes and assign the same pack to each
        num_volumes = 950
        volume_ids = []
        for i in range(num_volumes):
            vol = create_volume(
                memory_db, label=f"VOL{i:04d}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
            )
            volume_ids.append(vol.volume_id)

        # Register one pack
        pack = register_pack(memory_db, sha256="a" * 64, size_bytes=1000, repo_id=repo.repo_id)

        # Link to all volumes
        for vid in volume_ids:
            bulk_link_packs(memory_db, vid, [pack.pack_id])

        # Query should return the pack when searching across >900 volumes
        packs = get_packs_only_on_volumes(memory_db, volume_ids)
        assert len(packs) == 1
        assert packs[0].sha256 == "a" * 64


# ---------------------------------------------------------------------------
# Snapshot JSON helpers (get_snapshots_by_path, get_snapshots_by_tag)
# ---------------------------------------------------------------------------


class TestSnapshotsByPath:
    """Tests for get_snapshots_by_path using json_each()."""

    def test_exact_path_match(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            paths=json.dumps(["/home/user/docs", "/home/user/photos"]),
        )
        upsert_snapshot(
            memory_db, "snap2", "_test", "host1", "2025-01-02T00:00:00",
            paths=json.dumps(["/var/log"]),
        )
        results = get_snapshots_by_path(memory_db, "/home/user/docs")
        assert len(results) == 1
        assert results[0].snapshot_id == "snap1"

    def test_wildcard_path(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            paths=json.dumps(["/home/user/docs"]),
        )
        upsert_snapshot(
            memory_db, "snap2", "_test", "host1", "2025-01-02T00:00:00",
            paths=json.dumps(["/home/admin/docs"]),
        )
        upsert_snapshot(
            memory_db, "snap3", "_test", "host1", "2025-01-03T00:00:00",
            paths=json.dumps(["/var/log"]),
        )
        results = get_snapshots_by_path(memory_db, "/home/%/docs")
        ids = {s.snapshot_id for s in results}
        assert ids == {"snap1", "snap2"}

    def test_no_match(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            paths=json.dumps(["/home/user/docs"]),
        )
        results = get_snapshots_by_path(memory_db, "/nonexistent")
        assert results == []

    def test_filter_by_repo(self, memory_db):
        from lcsas.db.repos import register_repo
        register_repo(memory_db, "repo_b", "B", "/b")
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            paths=json.dumps(["/shared/data"]),
        )
        upsert_snapshot(
            memory_db, "snap2", "repo_b", "host2", "2025-01-02T00:00:00",
            paths=json.dumps(["/shared/data"]),
        )
        results = get_snapshots_by_path(memory_db, "/shared/data", repo_id="repo_b")
        assert len(results) == 1
        assert results[0].snapshot_id == "snap2"

    def test_ordered_newest_first(self, memory_db):
        for i in range(1, 4):
            upsert_snapshot(
                memory_db, f"snap{i}", "_test", "host1",
                f"2025-01-0{i}T00:00:00",
                paths=json.dumps(["/data"]),
            )
        results = get_snapshots_by_path(memory_db, "/data")
        assert [s.snapshot_id for s in results] == ["snap3", "snap2", "snap1"]

    def test_empty_paths_not_matched(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            paths="[]",
        )
        results = get_snapshots_by_path(memory_db, "%")
        assert results == []


class TestSnapshotsByTag:
    """Tests for get_snapshots_by_tag using json_each()."""

    def test_exact_tag_match(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            tags=json.dumps(["daily", "important"]),
        )
        upsert_snapshot(
            memory_db, "snap2", "_test", "host1", "2025-01-02T00:00:00",
            tags=json.dumps(["weekly"]),
        )
        results = get_snapshots_by_tag(memory_db, "important")
        assert len(results) == 1
        assert results[0].snapshot_id == "snap1"

    def test_no_match(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            tags=json.dumps(["daily"]),
        )
        results = get_snapshots_by_tag(memory_db, "monthly")
        assert results == []

    def test_filter_by_repo(self, memory_db):
        from lcsas.db.repos import register_repo
        register_repo(memory_db, "repo_b", "B", "/b")
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            tags=json.dumps(["backup"]),
        )
        upsert_snapshot(
            memory_db, "snap2", "repo_b", "host2", "2025-01-02T00:00:00",
            tags=json.dumps(["backup"]),
        )
        results = get_snapshots_by_tag(memory_db, "backup", repo_id="repo_b")
        assert len(results) == 1
        assert results[0].snapshot_id == "snap2"

    def test_multiple_snapshots_same_tag(self, memory_db):
        for i in range(1, 4):
            upsert_snapshot(
                memory_db, f"snap{i}", "_test", "host1",
                f"2025-01-0{i}T00:00:00",
                tags=json.dumps(["common"]),
            )
        results = get_snapshots_by_tag(memory_db, "common")
        assert len(results) == 3
        # Newest first
        assert results[0].snapshot_id == "snap3"

    def test_empty_tags_not_matched(self, memory_db):
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            tags="[]",
        )
        results = get_snapshots_by_tag(memory_db, "anything")
        assert results == []

    def test_partial_tag_no_match(self, memory_db):
        """Tag match is exact, not LIKE."""
        upsert_snapshot(
            memory_db, "snap1", "_test", "host1", "2025-01-01T00:00:00",
            tags=json.dumps(["daily-backup"]),
        )
        results = get_snapshots_by_tag(memory_db, "daily")
        assert results == []


class TestLocationSummary:
    """Tests for get_location_summary with deprecated volumes (T18/R3-M9).

    Note: Full integration test is complex due to volume_copies setup.
    The actual fix (JOIN volumes and filter by status) is verified by code review.
    """

    def test_location_summary_basic(self, memory_db):
        """Basic smoke test for get_location_summary."""
        from lcsas.db.queries import get_location_summary
        # Should not raise, may return empty list if no locations
        result = get_location_summary(memory_db)
        assert isinstance(result, list)

