"""Tests for volumes CRUD operations."""

from __future__ import annotations

import sqlite3

import pytest

from lcsas.db.volumes import (
    check_deprecation_safe,
    create_volume,
    delete_volume,
    get_volume_by_id,
    get_volume_by_label,
    get_volume_by_uuid,
    list_volumes,
    mark_closed,
    update_status,
    update_used_bytes,
)
from lcsas.utils.labels import generate_uuid


class TestVolumesCRUD:
    def test_create_and_fetch(self, memory_db):
        vol = create_volume(
            memory_db, label="TEST_001", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        assert vol.label == "TEST_001"
        assert vol.status == "STAGING"
        assert vol.used_bytes == 0

    def test_get_by_label(self, memory_db):
        create_volume(
            memory_db, label="VOL_A", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        vol = get_volume_by_label(memory_db, "VOL_A")
        assert vol is not None
        assert vol.label == "VOL_A"

    def test_get_by_label_missing(self, memory_db):
        assert get_volume_by_label(memory_db, "NOPE") is None

    def test_get_by_uuid(self, memory_db):
        uid = generate_uuid()
        create_volume(
            memory_db, label="VOL_U", uuid=uid,
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        vol = get_volume_by_uuid(memory_db, uid)
        assert vol is not None
        assert vol.uuid == uid

    def test_update_status(self, memory_db):
        vol = create_volume(
            memory_db, label="VOL_S", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        # Step through valid transitions: STAGING → BURNING → BURNED → VERIFIED
        update_status(memory_db, vol.volume_id, "BURNING")
        update_status(memory_db, vol.volume_id, "BURNED")
        update_status(memory_db, vol.volume_id, "VERIFIED")
        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.status == "VERIFIED"

    def test_update_status_invalid_transition(self, memory_db):
        """Skipping states is rejected by transition enforcement."""
        vol = create_volume(
            memory_db, label="VOL_TRANS", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        with pytest.raises(ValueError, match="Invalid status transition"):
            update_status(memory_db, vol.volume_id, "VERIFIED")

    def test_update_status_force(self, memory_db):
        """force=True bypasses transition enforcement."""
        vol = create_volume(
            memory_db, label="VOL_FORCE", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        update_status(memory_db, vol.volume_id, "VERIFIED", force=True)
        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.status == "VERIFIED"

    def test_mark_closed(self, memory_db):
        vol = create_volume(
            memory_db, label="VOL_C", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        assert vol.closed_at is None
        mark_closed(memory_db, vol.volume_id)
        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.closed_at is not None

    def test_update_used_bytes(self, memory_db):
        vol = create_volume(
            memory_db, label="VOL_UB", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        update_used_bytes(memory_db, vol.volume_id, 1_000_000)
        updated = get_volume_by_id(memory_db, vol.volume_id)
        assert updated.used_bytes == 1_000_000

    def test_list_all(self, memory_db):
        for i in range(3):
            create_volume(
                memory_db, label=f"LIST_{i}", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
            )
        vols = list_volumes(memory_db)
        assert len(vols) == 3

    def test_list_filtered(self, memory_db):
        create_volume(
            memory_db, label="F1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="STAGING",
        )
        create_volume(
            memory_db, label="F2", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        staging = list_volumes(memory_db, status_filter="STAGING")
        assert len(staging) == 1
        assert staging[0].label == "F1"

    def test_delete(self, memory_db):
        vol = create_volume(
            memory_db, label="DEL_ME", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        delete_volume(memory_db, vol.volume_id)
        assert get_volume_by_label(memory_db, "DEL_ME") is None

    def test_get_by_id_not_found(self, memory_db):
        with pytest.raises(ValueError, match="not found"):
            get_volume_by_id(memory_db, 9999)

    def test_duplicate_label_fails(self, memory_db):
        create_volume(
            memory_db, label="DUP", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        with pytest.raises(sqlite3.IntegrityError):
            create_volume(
                memory_db, label="DUP", uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
            )


class TestDeprecationSafety:
    """Tests for check_deprecation_safe()."""

    def test_safe_when_pack_on_other_volume(self, memory_db):
        """Pack on two volumes → deprecating one is safe."""
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        v1 = create_volume(memory_db, label="V1", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        v2 = create_volume(memory_db, label="V2", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        # Advance both to BURNED
        update_status(memory_db, v1.volume_id, "BURNING")
        update_status(memory_db, v1.volume_id, "BURNED")
        update_status(memory_db, v2.volume_id, "BURNING")
        update_status(memory_db, v2.volume_id, "BURNED")

        p = register_pack(memory_db, sha256="deadbeef" * 8, size_bytes=1000,
                          repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, v2.volume_id, [p.pack_id])

        at_risk = check_deprecation_safe(memory_db, v1.volume_id)
        assert at_risk == []

    def test_unsafe_when_only_copy(self, memory_db):
        """Pack on one volume only → deprecation is unsafe."""
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        v1 = create_volume(memory_db, label="ONLY", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        update_status(memory_db, v1.volume_id, "BURNING")
        update_status(memory_db, v1.volume_id, "BURNED")

        sha = "abcd1234" * 8
        p = register_pack(memory_db, sha256=sha, size_bytes=500,
                          repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])

        at_risk = check_deprecation_safe(memory_db, v1.volume_id)
        assert sha in at_risk

    def test_update_status_blocks_unsafe_deprecation(self, memory_db):
        """update_status raises ValueError when deprecation is unsafe."""
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        v1 = create_volume(memory_db, label="BLOCK", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        update_status(memory_db, v1.volume_id, "BURNING")
        update_status(memory_db, v1.volume_id, "BURNED")
        update_status(memory_db, v1.volume_id, "VERIFIED")

        sha = "babe0000" * 8
        p = register_pack(memory_db, sha256=sha, size_bytes=300,
                          repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])

        with pytest.raises(ValueError, match="unreplicated"):
            update_status(memory_db, v1.volume_id, "DEPRECATED")

    def test_force_overrides_safety_check(self, memory_db):
        """force=True bypasses deprecation safety."""
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        v1 = create_volume(memory_db, label="FORCE_D", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        update_status(memory_db, v1.volume_id, "BURNING")
        update_status(memory_db, v1.volume_id, "BURNED")
        update_status(memory_db, v1.volume_id, "VERIFIED")

        p = register_pack(memory_db, sha256="f00d0000" * 8, size_bytes=300,
                          repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])

        update_status(memory_db, v1.volume_id, "DEPRECATED", force=True)
        vol = get_volume_by_label(memory_db, "FORCE_D")
        assert vol.status == "DEPRECATED"


    def test_deprecation_guard_ignores_volumes_without_active_copies(self, memory_db):
        """BURN-10: a volume whose only copy is DESTROYED is not a replica.

        VOL_B keeps its BURNED status (a loss recorded before
        auto-demotion existed, or a rebuilt catalog); only the copy row
        says the disc is gone — the guard must see through the status.
        """
        from lcsas.db.locations import create_location
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volume_packs import bulk_link_packs

        create_location(memory_db, "Home_Shelf")
        create_location(memory_db, "Offsite_Safe")

        va = create_volume(memory_db, label="VOL_A", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        vb = create_volume(memory_db, label="VOL_B", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        for v in (va, vb):
            update_status(memory_db, v.volume_id, "BURNING")
            update_status(memory_db, v.volume_id, "BURNED")
        add_volume_copy(memory_db, va.volume_id, "Home_Shelf")
        add_volume_copy(memory_db, vb.volume_id, "Offsite_Safe")

        sha = "0badcafe" * 8
        p = register_pack(memory_db, sha256=sha, size_bytes=100,
                          repo_id="_test")
        bulk_link_packs(memory_db, va.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, vb.volume_id, [p.pack_id])

        # Both replicas live → deprecating VOL_A is safe.
        assert check_deprecation_safe(memory_db, va.volume_id) == []

        # Kill VOL_B's only copy at the row level WITHOUT demoting the
        # volume (pre-BURN-10 catalogs recorded exactly this state).
        memory_db.execute(
            "UPDATE volume_copies SET status = 'DESTROYED' WHERE volume_id = ?",
            (vb.volume_id,),
        )
        memory_db.commit()

        at_risk = check_deprecation_safe(memory_db, va.volume_id)
        assert sha in at_risk

    def test_deprecation_guard_legacy_volume_without_copy_rows_counts(self, memory_db):
        """BURN-10 carve-out: zero copy rows → the volume counts by status.

        Volumes burned before copies were recorded (and skip_burn
        fixtures, and rebuilt catalogs) have no volume_copies rows;
        treating them as dead would mass-trip the guard.
        """
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        va = create_volume(memory_db, label="VOL_A", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        vb = create_volume(memory_db, label="VOL_B", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        for v in (va, vb):
            update_status(memory_db, v.volume_id, "BURNING")
            update_status(memory_db, v.volume_id, "BURNED")
        update_status(memory_db, vb.volume_id, "VERIFIED")

        p = register_pack(memory_db, sha256="1e9acafe" * 8, size_bytes=100,
                          repo_id="_test")
        bulk_link_packs(memory_db, va.volume_id, [p.pack_id])
        bulk_link_packs(memory_db, vb.volume_id, [p.pack_id])

        # VOL_B has no copy rows at all — it still counts as a replica.
        assert check_deprecation_safe(memory_db, va.volume_id) == []

    def test_deprecate_inside_outer_transaction(self, memory_db):
        """update_status(DEPRECATED) works inside outer transaction (T20/R3-H2)."""
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import bulk_link_packs

        v1 = create_volume(memory_db, label="TXN_TEST", uuid=generate_uuid(),
                           media_type="BD25", capacity_bytes=25_000_000_000)
        update_status(memory_db, v1.volume_id, "BURNING")
        update_status(memory_db, v1.volume_id, "BURNED")
        update_status(memory_db, v1.volume_id, "VERIFIED")

        p = register_pack(memory_db, sha256="dead0000" * 8, size_bytes=300,
                          repo_id="_test")
        bulk_link_packs(memory_db, v1.volume_id, [p.pack_id])

        # Start an outer transaction
        with memory_db:
            # Call deprecate inside the transaction - should not raise
            # "cannot start a transaction within a transaction"
            update_status(memory_db, v1.volume_id, "DEPRECATED", force=True)
            vol = get_volume_by_id(memory_db, v1.volume_id)
            assert vol.status == "DEPRECATED"
        # Transaction committed successfully

