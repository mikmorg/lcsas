"""Tests for db/volume_packs.py — junction table CRUD."""

from __future__ import annotations

import contextlib

from lcsas.db.packs import register_pack
from lcsas.db.volume_packs import (
    bulk_link_packs,
    get_pack_ids_for_volume,
    get_volume_ids_for_pack,
    link_pack_to_volume,
    unlink_pack_from_volume,
)
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid


def _make_vol(conn, label="VOL_001"):
    return create_volume(
        conn, label=label, uuid=generate_uuid(),
        media_type="BD25", capacity_bytes=25_000_000_000,
    )


def _make_pack(conn, sha="abc123", size=1000, repo="repo_a"):
    from lcsas.db.repos import register_repo
    with contextlib.suppress(Exception):
        register_repo(conn, repo, repo, f"/mnt/{repo}")
    return register_pack(conn, sha256=sha, size_bytes=size, repo_id=repo)


class TestLinkPackToVolume:
    def test_basic_link(self, memory_db):
        vol = _make_vol(memory_db)
        pack = _make_pack(memory_db)
        link_pack_to_volume(memory_db, vol.volume_id, pack.pack_id)

        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert pack.pack_id in ids

    def test_duplicate_link_idempotent(self, memory_db):
        """INSERT OR IGNORE means duplicates are silently ignored."""
        vol = _make_vol(memory_db)
        pack = _make_pack(memory_db)
        link_pack_to_volume(memory_db, vol.volume_id, pack.pack_id)
        link_pack_to_volume(memory_db, vol.volume_id, pack.pack_id)

        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert ids.count(pack.pack_id) == 1

    def test_multiple_packs_on_volume(self, memory_db):
        vol = _make_vol(memory_db)
        p1 = _make_pack(memory_db, sha="sha1")
        p2 = _make_pack(memory_db, sha="sha2")
        link_pack_to_volume(memory_db, vol.volume_id, p1.pack_id)
        link_pack_to_volume(memory_db, vol.volume_id, p2.pack_id)

        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert len(ids) == 2


class TestUnlinkPackFromVolume:
    def test_unlink_existing(self, memory_db):
        vol = _make_vol(memory_db)
        pack = _make_pack(memory_db)
        link_pack_to_volume(memory_db, vol.volume_id, pack.pack_id)
        unlink_pack_from_volume(memory_db, vol.volume_id, pack.pack_id)

        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert pack.pack_id not in ids

    def test_unlink_nonexistent_noop(self, memory_db):
        """Unlinking a non-existent association doesn't raise."""
        vol = _make_vol(memory_db)
        unlink_pack_from_volume(memory_db, vol.volume_id, 99999)
        # No error, no effect


class TestGetPackIdsForVolume:
    def test_empty_volume(self, memory_db):
        vol = _make_vol(memory_db)
        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert ids == []

    def test_returns_correct_ids(self, memory_db):
        vol = _make_vol(memory_db)
        p1 = _make_pack(memory_db, sha="sha_x")
        p2 = _make_pack(memory_db, sha="sha_y")
        bulk_link_packs(memory_db, vol.volume_id, [p1.pack_id, p2.pack_id])

        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert set(ids) == {p1.pack_id, p2.pack_id}


class TestGetVolumeIdsForPack:
    def test_pack_on_no_volumes(self, memory_db):
        pack = _make_pack(memory_db)
        ids = get_volume_ids_for_pack(memory_db, pack.pack_id)
        assert ids == []

    def test_pack_on_multiple_volumes(self, memory_db):
        v1 = _make_vol(memory_db, label="V1")
        v2 = _make_vol(memory_db, label="V2")
        pack = _make_pack(memory_db)
        link_pack_to_volume(memory_db, v1.volume_id, pack.pack_id)
        link_pack_to_volume(memory_db, v2.volume_id, pack.pack_id)

        ids = get_volume_ids_for_pack(memory_db, pack.pack_id)
        assert set(ids) == {v1.volume_id, v2.volume_id}


class TestBulkLinkPacks:
    def test_bulk_link(self, memory_db):
        vol = _make_vol(memory_db)
        packs = [_make_pack(memory_db, sha=f"bulk_{i}") for i in range(5)]
        pack_ids = [p.pack_id for p in packs]
        bulk_link_packs(memory_db, vol.volume_id, pack_ids)

        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert set(ids) == set(pack_ids)

    def test_bulk_link_empty_list(self, memory_db):
        vol = _make_vol(memory_db)
        bulk_link_packs(memory_db, vol.volume_id, [])
        ids = get_pack_ids_for_volume(memory_db, vol.volume_id)
        assert ids == []
