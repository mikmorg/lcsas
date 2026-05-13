"""Tests for db/volume_copies.py — volume copy tracking."""

from __future__ import annotations

import json

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.locations import create_location
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import (
    add_volume_copy,
    deprecate_copy,
    destroy_copy,
    get_copies_at_location,
    get_copies_for_volume,
    move_volume_copy,
)
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid


@pytest.fixture
def conn():
    c = get_memory_connection()
    create_all(c)
    create_location(c, "Home_Shelf")
    create_location(c, "Offsite_Safe")
    create_location(c, "Bank_Vault")
    yield c
    c.close()


@pytest.fixture
def volume(conn):
    return create_volume(
        conn, label="TEST_001", uuid=generate_uuid(),
        media_type="TEST_TINY", capacity_bytes=1_000_000,
    )


class TestVolumeCopyCRUD:
    def test_add_and_get(self, conn, volume):
        copy = add_volume_copy(conn, volume.volume_id, "Home_Shelf")
        assert copy.volume_id == volume.volume_id
        assert copy.location == "Home_Shelf"
        assert copy.status == "ACTIVE"

    def test_multiple_copies_same_volume(self, conn, volume):
        add_volume_copy(conn, volume.volume_id, "Home_Shelf")
        add_volume_copy(conn, volume.volume_id, "Offsite_Safe")

        copies = get_copies_for_volume(conn, volume.volume_id)
        assert len(copies) == 2
        locations = {c.location for c in copies}
        assert locations == {"Home_Shelf", "Offsite_Safe"}

    def test_duplicate_location_upserts(self, conn, volume):
        """Re-burning at the same location updates instead of raising."""
        add_volume_copy(conn, volume.volume_id, "Home_Shelf",
                        notes="first burn")
        copy2 = add_volume_copy(conn, volume.volume_id, "Home_Shelf",
                                notes="re-burn")
        # Should be UPSERT (same row), not a new row
        copies = get_copies_for_volume(conn, volume.volume_id)
        assert len(copies) == 1
        assert copies[0].notes == "re-burn"
        # Verify return value is correct on UPSERT (not dependent on lastrowid)
        assert copy2.volume_id == volume.volume_id
        assert copy2.location == "Home_Shelf"
        assert copy2.notes == "re-burn"
        assert copy2.status == "ACTIVE"

    def test_get_copies_at_location(self, conn):
        v1 = create_volume(conn, "V1", generate_uuid(), "TEST_TINY", 1000000)
        v2 = create_volume(conn, "V2", generate_uuid(), "TEST_TINY", 1000000)
        add_volume_copy(conn, v1.volume_id, "Home_Shelf")
        add_volume_copy(conn, v2.volume_id, "Home_Shelf")
        add_volume_copy(conn, v1.volume_id, "Offsite_Safe")

        at_home = get_copies_at_location(conn, "Home_Shelf")
        assert len(at_home) == 2

        at_offsite = get_copies_at_location(conn, "Offsite_Safe")
        assert len(at_offsite) == 1

    def test_move_volume_copy(self, conn, volume):
        add_volume_copy(conn, volume.volume_id, "Home_Shelf")
        move_volume_copy(conn, volume.volume_id, "Home_Shelf", "Offsite_Safe")

        copies = get_copies_for_volume(conn, volume.volume_id)
        assert len(copies) == 1
        assert copies[0].location == "Offsite_Safe"
        assert "Moved from Home_Shelf" in copies[0].notes

    def test_move_nonexistent_raises(self, conn, volume):
        with pytest.raises(ValueError, match="No active copy"):
            move_volume_copy(conn, volume.volume_id, "Home_Shelf", "Offsite_Safe")

    def test_move_emits_location_move_event(self, conn, volume):
        """move_volume_copy must emit exactly one LOCATION_MOVE event row
        capturing the from and to locations (audit trail — issue #16)."""
        add_volume_copy(conn, volume.volume_id, "Home_Shelf")

        move_volume_copy(conn, volume.volume_id, "Home_Shelf", "Offsite_Safe")

        move_events = get_events_for_volume(
            conn, volume.volume_id, event_type="LOCATION_MOVE"
        )
        assert len(move_events) == 1
        ev = move_events[0]
        assert ev.volume_id == volume.volume_id
        assert ev.event_type == "LOCATION_MOVE"
        # The new location is recorded on the dedicated column …
        assert ev.location == "Offsite_Safe"
        # … and the original location is captured in detail (JSON payload).
        payload = json.loads(ev.detail)
        assert payload["from_location"] == "Home_Shelf"
        assert payload["to_location"] == "Offsite_Safe"
        # Timestamp must be populated.
        assert ev.event_date

    def test_deprecate_copy(self, conn, volume):
        add_volume_copy(conn, volume.volume_id, "Home_Shelf")
        deprecate_copy(conn, volume.volume_id, "Home_Shelf")

        active = get_copies_for_volume(conn, volume.volume_id, active_only=True)
        assert len(active) == 0

        all_copies = get_copies_for_volume(conn, volume.volume_id, active_only=False)
        assert len(all_copies) == 1
        assert all_copies[0].status == "DEPRECATED"

    def test_destroy_copy(self, conn, volume):
        add_volume_copy(conn, volume.volume_id, "Home_Shelf")
        destroy_copy(conn, volume.volume_id, "Home_Shelf")

        all_copies = get_copies_for_volume(conn, volume.volume_id, active_only=False)
        assert all_copies[0].status == "DESTROYED"
