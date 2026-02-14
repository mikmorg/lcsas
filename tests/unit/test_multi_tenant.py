"""Multi-tenant (multi-repo) isolation and correctness tests.

Validates that repos are properly isolated in the catalog, packs are
scoped by repo_id, unarchived queries filter correctly, and the burn +
restore pipeline respects tenant boundaries.
"""

from __future__ import annotations

import pytest

from lcsas.db.models import Pack
from lcsas.db.packs import get_pack_by_sha256, mark_pruned, register_pack
from lcsas.db.queries import (
    get_archive_status_summary,
    get_packs_for_volume,
    get_redundancy_report,
    get_total_unarchived_bytes,
    get_unarchived_packs,
    get_volumes_for_pack,
)
from lcsas.db.repos import delete_repo, get_repo, list_repos, register_repo
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.restore.planner import RestorePlanner
from lcsas.utils.labels import generate_uuid

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def multi_tenant_db(memory_db):
    """DB with 5 repos, distinct packs per repo, 3 volumes with overlap.

    Repos: alpha, beta, gamma, delta, epsilon
    Packs:
      - alpha:   a01..a05  (5 packs, 1000-5000 bytes)
      - beta:    b01..b05  (5 packs, 1000-5000 bytes)
      - gamma:   g01..g03  (3 packs, 1000-3000 bytes)
      - delta:   d01..d02  (2 packs, 1000-2000 bytes)
      - epsilon: (no packs yet)

    Volumes:
      - V_MIX1 (VERIFIED): alpha a01-a03 + beta b01-b02  (mixed repos)
      - V_MIX2 (VERIFIED): alpha a04-a05 + gamma g01-g03  (mixed repos)
      - V_BETA (VERIFIED): beta b01-b05  (single-repo, overlaps V_MIX1)
      Unarchived: delta d01-d02
    """
    conn = memory_db

    for rid, name in [
        ("alpha", "Alpha Repo"),
        ("beta", "Beta Repo"),
        ("gamma", "Gamma Repo"),
        ("delta", "Delta Repo"),
        ("epsilon", "Epsilon Repo"),
    ]:
        register_repo(conn, rid, name, f"/mnt/mirror/{rid}")

    # Register packs
    packs: dict[str, Pack] = {}
    for prefix, repo_id, count in [
        ("a", "alpha", 5),
        ("b", "beta", 5),
        ("g", "gamma", 3),
        ("d", "delta", 2),
    ]:
        for i in range(1, count + 1):
            sha = f"{prefix}{i:02d}_sha256"
            p = register_pack(conn, sha256=sha, size_bytes=1000 * i, repo_id=repo_id)
            packs[sha] = p

    # Create volumes
    v_mix1 = create_volume(
        conn, label="V_MIX1", uuid=generate_uuid(),
        media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
    )
    v_mix2 = create_volume(
        conn, label="V_MIX2", uuid=generate_uuid(),
        media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
    )
    v_beta = create_volume(
        conn, label="V_BETA", uuid=generate_uuid(),
        media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
    )

    # V_MIX1: alpha a01-a03, beta b01-b02
    bulk_link_packs(conn, v_mix1.volume_id, [
        packs["a01_sha256"].pack_id, packs["a02_sha256"].pack_id,
        packs["a03_sha256"].pack_id,
        packs["b01_sha256"].pack_id, packs["b02_sha256"].pack_id,
    ])
    # V_MIX2: alpha a04-a05, gamma g01-g03
    bulk_link_packs(conn, v_mix2.volume_id, [
        packs["a04_sha256"].pack_id, packs["a05_sha256"].pack_id,
        packs["g01_sha256"].pack_id, packs["g02_sha256"].pack_id,
        packs["g03_sha256"].pack_id,
    ])
    # V_BETA: beta b01-b05 (b01-b02 redundant with V_MIX1)
    bulk_link_packs(conn, v_beta.volume_id, [
        packs["b01_sha256"].pack_id, packs["b02_sha256"].pack_id,
        packs["b03_sha256"].pack_id, packs["b04_sha256"].pack_id,
        packs["b05_sha256"].pack_id,
    ])

    return conn


# =========================================================================
# Repo isolation
# =========================================================================


class TestRepoIsolation:
    """Test that repositories are independent tenants."""

    def test_repos_registered_independently(self, multi_tenant_db):
        repos = list_repos(multi_tenant_db)
        names = {r.name for r in repos}
        assert names == {"Alpha Repo", "Beta Repo", "Gamma Repo",
                         "Delta Repo", "Epsilon Repo"}

    def test_repo_retrieval_by_id(self, multi_tenant_db):
        repo = get_repo(multi_tenant_db, "gamma")
        assert repo.name == "Gamma Repo"
        assert repo.mirror_path == "/mnt/mirror/gamma"

    def test_deleting_repo_does_not_affect_others(self, multi_tenant_db):
        delete_repo(multi_tenant_db, "epsilon")
        repos = list_repos(multi_tenant_db)
        assert len(repos) == 4
        ids = {r.repo_id for r in repos}
        assert "epsilon" not in ids
        # Other repos intact
        assert "alpha" in ids
        assert "beta" in ids

    def test_packs_scoped_to_repo(self, multi_tenant_db):
        """Each pack's repo_id matches the repo that owns it."""
        for prefix, repo_id, count in [("a", "alpha", 5), ("b", "beta", 5),
                                        ("g", "gamma", 3), ("d", "delta", 2)]:
            for i in range(1, count + 1):
                sha = f"{prefix}{i:02d}_sha256"
                p = get_pack_by_sha256(multi_tenant_db, sha)
                assert p is not None
                assert p.repo_id == repo_id, (
                    f"{sha} should belong to {repo_id}, got {p.repo_id}"
                )

    def test_empty_repo_has_no_packs(self, multi_tenant_db):
        """Epsilon repo was registered but has no packs."""
        unarchived = get_unarchived_packs(multi_tenant_db, repo_id="epsilon")
        assert unarchived == []


# =========================================================================
# Per-repo unarchived queries
# =========================================================================


class TestPerRepoUnarchived:
    """Unarchived pack queries correctly filter by repo_id."""

    def test_all_repos_unarchived(self, multi_tenant_db):
        """Only delta's 2 packs are unarchived across all repos."""
        unarchived = get_unarchived_packs(multi_tenant_db)
        shas = {p.sha256 for p in unarchived}
        assert shas == {"d01_sha256", "d02_sha256"}

    def test_alpha_fully_archived(self, multi_tenant_db):
        unarchived = get_unarchived_packs(multi_tenant_db, repo_id="alpha")
        assert len(unarchived) == 0

    def test_beta_fully_archived(self, multi_tenant_db):
        unarchived = get_unarchived_packs(multi_tenant_db, repo_id="beta")
        assert len(unarchived) == 0

    def test_gamma_fully_archived(self, multi_tenant_db):
        unarchived = get_unarchived_packs(multi_tenant_db, repo_id="gamma")
        assert len(unarchived) == 0

    def test_delta_has_unarchived(self, multi_tenant_db):
        unarchived = get_unarchived_packs(multi_tenant_db, repo_id="delta")
        shas = {p.sha256 for p in unarchived}
        assert shas == {"d01_sha256", "d02_sha256"}

    def test_unarchived_bytes_per_repo(self, multi_tenant_db):
        assert get_total_unarchived_bytes(multi_tenant_db, repo_id="alpha") == 0
        assert get_total_unarchived_bytes(multi_tenant_db, repo_id="beta") == 0
        # delta: d01=1000, d02=2000
        assert get_total_unarchived_bytes(multi_tenant_db, repo_id="delta") == 3000

    def test_unarchived_bytes_all(self, multi_tenant_db):
        total = get_total_unarchived_bytes(multi_tenant_db)
        assert total == 3000


# =========================================================================
# Mixed-repo volumes
# =========================================================================


class TestMixedRepoVolumes:
    """Test volumes containing packs from multiple repos."""

    def test_v_mix1_has_multi_repo_packs(self, multi_tenant_db):
        from lcsas.db.volumes import get_volume_by_label

        vol = get_volume_by_label(multi_tenant_db, "V_MIX1")
        packs = get_packs_for_volume(multi_tenant_db, vol.volume_id)
        repos = {p.repo_id for p in packs}
        assert repos == {"alpha", "beta"}
        assert len(packs) == 5  # a01-a03 + b01-b02

    def test_v_mix2_has_multi_repo_packs(self, multi_tenant_db):
        from lcsas.db.volumes import get_volume_by_label

        vol = get_volume_by_label(multi_tenant_db, "V_MIX2")
        packs = get_packs_for_volume(multi_tenant_db, vol.volume_id)
        repos = {p.repo_id for p in packs}
        assert repos == {"alpha", "gamma"}
        assert len(packs) == 5  # a04-a05 + g01-g03

    def test_v_beta_is_single_repo(self, multi_tenant_db):
        from lcsas.db.volumes import get_volume_by_label

        vol = get_volume_by_label(multi_tenant_db, "V_BETA")
        packs = get_packs_for_volume(multi_tenant_db, vol.volume_id)
        repos = {p.repo_id for p in packs}
        assert repos == {"beta"}
        assert len(packs) == 5

    def test_pack_on_multiple_volumes_different_repos(self, multi_tenant_db):
        """Beta b01 is on V_MIX1 and V_BETA — 2 copies, same repo."""
        p = get_pack_by_sha256(multi_tenant_db, "b01_sha256")
        vols = get_volumes_for_pack(multi_tenant_db, p.pack_id)
        labels = {v.label for v in vols}
        assert labels == {"V_MIX1", "V_BETA"}


# =========================================================================
# DeltaAnalyzer per-repo
# =========================================================================


class TestDeltaAnalyzerMultiTenant:
    """DeltaAnalyzer correctly scopes registration and unarchived queries."""

    def test_register_new_packs_for_specific_repo(self, memory_db):
        register_repo(memory_db, "proj_a", "Project A", "/mnt/a")
        register_repo(memory_db, "proj_b", "Project B", "/mnt/b")

        scanner_a = {"pa1_hash": 100, "pa2_hash": 200}
        delta_a = DeltaAnalyzer(memory_db, scanner_a, repo_id="proj_a")
        new_a = delta_a.register_new_packs()
        assert len(new_a) == 2
        assert all(p.repo_id == "proj_a" for p in new_a)

        scanner_b = {"pb1_hash": 300}
        delta_b = DeltaAnalyzer(memory_db, scanner_b, repo_id="proj_b")
        new_b = delta_b.register_new_packs()
        assert len(new_b) == 1
        assert new_b[0].repo_id == "proj_b"

    def test_unarchived_scoped_to_repo(self, memory_db):
        register_repo(memory_db, "proj_a", "Project A", "/mnt/a")
        register_repo(memory_db, "proj_b", "Project B", "/mnt/b")

        scanner_a = {"pa1_hash": 100, "pa2_hash": 200}
        scanner_b = {"pb1_hash": 300, "pb2_hash": 400}

        DeltaAnalyzer(memory_db, scanner_a, repo_id="proj_a").register_new_packs()
        DeltaAnalyzer(memory_db, scanner_b, repo_id="proj_b").register_new_packs()

        delta_a = DeltaAnalyzer(memory_db, scanner_a, repo_id="proj_a")
        assert len(delta_a.get_unarchived()) == 2
        assert delta_a.get_total_unarchived_bytes() == 300

        delta_b = DeltaAnalyzer(memory_db, scanner_b, repo_id="proj_b")
        assert len(delta_b.get_unarchived()) == 2
        assert delta_b.get_total_unarchived_bytes() == 700

    def test_archiving_one_repo_leaves_other_unarchived(self, memory_db):
        """Archiving all packs of proj_a shouldn't affect proj_b."""
        register_repo(memory_db, "proj_a", "Project A", "/mnt/a")
        register_repo(memory_db, "proj_b", "Project B", "/mnt/b")

        packs_a = DeltaAnalyzer(
            memory_db, {"pa1": 100}, repo_id="proj_a"
        ).register_new_packs()
        DeltaAnalyzer(
            memory_db, {"pb1": 200}, repo_id="proj_b"
        ).register_new_packs()

        # Archive proj_a's pack
        vol = create_volume(
            memory_db, label="VOL_A", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000, status="VERIFIED",
        )
        bulk_link_packs(memory_db, vol.volume_id, [packs_a[0].pack_id])

        # proj_a should have 0 unarchived
        assert len(get_unarchived_packs(memory_db, repo_id="proj_a")) == 0
        # proj_b should still have 1 unarchived
        assert len(get_unarchived_packs(memory_db, repo_id="proj_b")) == 1

    def test_shared_pack_content_different_repos(self, memory_db):
        """If two repos happen to have the same SHA-256 pack, register_pack
        returns the existing one (dedup). Both repos effectively share it."""
        register_repo(memory_db, "r1", "R1", "/mnt/r1")
        register_repo(memory_db, "r2", "R2", "/mnt/r2")

        p1 = register_pack(memory_db, sha256="shared_hash", size_bytes=500, repo_id="r1")
        p2 = register_pack(memory_db, sha256="shared_hash", size_bytes=500, repo_id="r2")

        # register_pack deduplicates by SHA-256 — returns the same pack
        assert p1.pack_id == p2.pack_id
        # The repo_id stays as the first registrant's
        assert p1.repo_id == "r1"


# =========================================================================
# Redundancy report across repos
# =========================================================================


class TestRedundancyMultiTenant:

    def test_beta_packs_have_mixed_redundancy(self, multi_tenant_db):
        """b01, b02 have 2 copies (V_MIX1 + V_BETA).
        b03, b04, b05 have 1 copy (V_BETA only)."""
        under_2copies = get_redundancy_report(multi_tenant_db, min_copies=2)
        shas = {p.sha256 for p in under_2copies}
        # b03-b05 should appear (only 1 copy)
        assert "b03_sha256" in shas
        assert "b04_sha256" in shas
        assert "b05_sha256" in shas
        # b01-b02 should NOT appear (have 2 copies)
        assert "b01_sha256" not in shas
        assert "b02_sha256" not in shas

    def test_alpha_packs_single_copy(self, multi_tenant_db):
        """All alpha packs have exactly 1 copy each."""
        under_2copies = get_redundancy_report(multi_tenant_db, min_copies=2)
        shas = {p.sha256 for p in under_2copies}
        for i in range(1, 6):
            assert f"a{i:02d}_sha256" in shas

    def test_delta_packs_zero_copies(self, multi_tenant_db):
        """Delta packs are unarchived (0 copies)."""
        under_1copy = get_redundancy_report(multi_tenant_db, min_copies=1)
        shas = {p.sha256 for p in under_1copy}
        assert "d01_sha256" in shas
        assert "d02_sha256" in shas

    def test_min_copies_3_returns_everything_archived(self, multi_tenant_db):
        """No pack has 3+ copies, so all non-pruned packs should appear."""
        under_3copies = get_redundancy_report(multi_tenant_db, min_copies=3)
        assert len(under_3copies) == 15  # all 15 packs

    def test_pruned_packs_excluded_from_redundancy(self, multi_tenant_db):
        """Pruned packs should not appear in redundancy report."""
        p = get_pack_by_sha256(multi_tenant_db, "a01_sha256")
        mark_pruned(multi_tenant_db, p.pack_id)

        under_2copies = get_redundancy_report(multi_tenant_db, min_copies=2)
        shas = {p.sha256 for p in under_2copies}
        assert "a01_sha256" not in shas


# =========================================================================
# Archive status summary across repos
# =========================================================================


class TestArchiveStatusMultiTenant:

    def test_summary_all_repos(self, multi_tenant_db):
        summary = get_archive_status_summary(multi_tenant_db)
        assert summary["total"] == 15
        # alpha a01-a05 (5) + beta b01-b05 (5) + gamma g01-g03 (3) = 13 archived
        assert summary["archived"] == 13
        # delta d01-d02 = 2 unarchived
        assert summary["unarchived"] == 2
        assert summary["pruned"] == 0

    def test_pruning_updates_summary(self, multi_tenant_db):
        p = get_pack_by_sha256(multi_tenant_db, "d01_sha256")
        mark_pruned(multi_tenant_db, p.pack_id)

        summary = get_archive_status_summary(multi_tenant_db)
        assert summary["pruned"] == 1
        assert summary["unarchived"] == 1


# =========================================================================
# Pick-list generation across repos
# =========================================================================


class TestPickListMultiTenant:

    def test_pick_list_single_repo_packs(self, multi_tenant_db):
        """Pick list for only alpha packs."""
        planner = RestorePlanner(multi_tenant_db)
        pick = planner.generate_pick_list(
            ["a01_sha256", "a02_sha256", "a03_sha256", "a04_sha256", "a05_sha256"]
        )
        assert pick.missing_packs == []
        assert pick.total_packs == 5

    def test_pick_list_cross_repo(self, multi_tenant_db):
        """Pick list spanning alpha, beta, and gamma packs."""
        planner = RestorePlanner(multi_tenant_db)
        pick = planner.generate_pick_list(
            ["a01_sha256", "b03_sha256", "g02_sha256"]
        )
        assert pick.missing_packs == []
        assert pick.total_packs == 3
        # a01 from V_MIX1, b03 from V_BETA, g02 from V_MIX2
        all_labels = set(pick.volumes.keys())
        assert len(all_labels) >= 2  # At least 2 distinct volumes

    def test_pick_list_unarchived_pack_is_missing(self, multi_tenant_db):
        """Delta packs are unarchived, so they count as missing."""
        planner = RestorePlanner(multi_tenant_db)
        pick = planner.generate_pick_list(
            ["d01_sha256", "a01_sha256"]
        )
        assert "d01_sha256" in pick.missing_packs
        assert pick.total_packs == 1  # Only a01 found on volume

    def test_pick_list_nonexistent_pack(self, multi_tenant_db):
        planner = RestorePlanner(multi_tenant_db)
        pick = planner.generate_pick_list(["does_not_exist"])
        assert "does_not_exist" in pick.missing_packs
        assert pick.total_packs == 0
