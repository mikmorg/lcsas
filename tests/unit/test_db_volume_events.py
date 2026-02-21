"""Tests for the volume_events CRUD module (Phase 12 / D1)."""

from __future__ import annotations

import pytest

from lcsas.db.volume_events import (
    VALID_EVENT_TYPES,
    add_event,
    get_event,
    get_events_by_type,
    get_events_for_volume,
    get_latest_event,
)
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid


class TestAddEvent:
    def test_basic(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        ev = add_event(memory_db, vol.volume_id, "VERIFY_PASS", detail="ok")
        assert ev.event_id >= 1
        assert ev.volume_id == vol.volume_id
        assert ev.event_type == "VERIFY_PASS"
        assert ev.detail == "ok"
        assert ev.location is None

    def test_with_location(self, memory_db):
        from lcsas.db.locations import ensure_location
        ensure_location(memory_db, "Home_Shelf")
        vol = create_volume(
            memory_db, label="EV_LOC", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        ev = add_event(memory_db, vol.volume_id, "LOCATION_MOVE",
                       location="Home_Shelf", detail="Moved to shelf")
        assert ev.location == "Home_Shelf"
        assert ev.event_type == "LOCATION_MOVE"

    def test_invalid_type_raises(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_BAD", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        with pytest.raises(ValueError, match="Invalid event_type"):
            add_event(memory_db, vol.volume_id, "BOGUS")

    def test_all_valid_types(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_TYPES", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        for et in sorted(VALID_EVENT_TYPES):
            ev = add_event(memory_db, vol.volume_id, et)
            assert ev.event_type == et

    def test_custom_event_date(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_DATE", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        ev = add_event(memory_db, vol.volume_id, "NOTE",
                       event_date="2025-01-01T00:00:00+00:00")
        assert ev.event_date == "2025-01-01T00:00:00+00:00"


class TestGetEvent:
    def test_existing(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_GET", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        created = add_event(memory_db, vol.volume_id, "NOTE", detail="test")
        fetched = get_event(memory_db, created.event_id)
        assert fetched == created

    def test_not_found_raises(self, memory_db):
        with pytest.raises(ValueError, match="not found"):
            get_event(memory_db, 999)


class TestGetEventsForVolume:
    def test_returns_multiple_ordered(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_MULTI", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        add_event(memory_db, vol.volume_id, "VERIFY_PASS",
                  event_date="2025-01-01T00:00:00")
        add_event(memory_db, vol.volume_id, "ECC_REPAIR",
                  event_date="2025-06-01T00:00:00")
        add_event(memory_db, vol.volume_id, "VERIFY_PASS",
                  event_date="2025-12-01T00:00:00")

        events = get_events_for_volume(memory_db, vol.volume_id)
        assert len(events) == 3
        # Newest first
        assert events[0].event_date >= events[1].event_date
        assert events[1].event_date >= events[2].event_date

    def test_filter_by_type(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_FILT", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        add_event(memory_db, vol.volume_id, "VERIFY_PASS")
        add_event(memory_db, vol.volume_id, "ECC_REPAIR")
        add_event(memory_db, vol.volume_id, "VERIFY_PASS")

        only_verify = get_events_for_volume(memory_db, vol.volume_id,
                                            event_type="VERIFY_PASS")
        assert len(only_verify) == 2
        assert all(e.event_type == "VERIFY_PASS" for e in only_verify)

    def test_empty_volume(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_EMPTY", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        assert get_events_for_volume(memory_db, vol.volume_id) == []


class TestGetLatestEvent:
    def test_returns_newest(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_LATEST", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        add_event(memory_db, vol.volume_id, "NOTE",
                  detail="old", event_date="2025-01-01T00:00:00")
        add_event(memory_db, vol.volume_id, "NOTE",
                  detail="new", event_date="2025-12-01T00:00:00")

        latest = get_latest_event(memory_db, vol.volume_id)
        assert latest is not None
        assert latest.detail == "new"

    def test_filtered_by_type(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_LTYPE", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        add_event(memory_db, vol.volume_id, "VERIFY_PASS",
                  event_date="2025-01-01T00:00:00")
        add_event(memory_db, vol.volume_id, "ECC_REPAIR",
                  event_date="2025-12-01T00:00:00")

        latest = get_latest_event(memory_db, vol.volume_id,
                                  event_type="VERIFY_PASS")
        assert latest is not None
        assert latest.event_type == "VERIFY_PASS"

    def test_none_when_empty(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_NONE", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        assert get_latest_event(memory_db, vol.volume_id) is None


class TestGetEventsByType:
    def test_across_volumes(self, memory_db):
        v1 = create_volume(
            memory_db, label="EV_V1", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        v2 = create_volume(
            memory_db, label="EV_V2", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        add_event(memory_db, v1.volume_id, "VERIFY_FAIL", detail="bad sector")
        add_event(memory_db, v2.volume_id, "VERIFY_FAIL", detail="CRC error")

        events = get_events_by_type(memory_db, "VERIFY_FAIL")
        assert len(events) == 2

    def test_respects_limit(self, memory_db):
        vol = create_volume(
            memory_db, label="EV_LIM", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        for i in range(5):
            add_event(memory_db, vol.volume_id, "NOTE", detail=f"n{i}")

        events = get_events_by_type(memory_db, "NOTE", limit=3)
        assert len(events) == 3
