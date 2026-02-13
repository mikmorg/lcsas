"""Tests for volumes CRUD operations."""

from __future__ import annotations

import sqlite3

import pytest

from lcsas.db.volumes import (
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
        update_status(memory_db, vol.volume_id, "VERIFIED")
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
