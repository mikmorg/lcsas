"""Tests for location-aware queries in db/queries.py."""

from __future__ import annotations

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import register_pack
from lcsas.db.queries import (
    get_location_summary,
    get_packs_at_location,
    get_packs_missing_at_location,
    get_unarchived_or_missing_at_location,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid


@pytest.fixture
def conn():
    c = get_memory_connection()
    create_all(c)
    register_repo(c, "family", "Family", "/mnt/mirror/family")
    register_repo(c, "work", "Work", "/mnt/mirror/work")
    create_location(c, "Home_Shelf")
    create_location(c, "Offsite_Safe")
    yield c
    c.close()


def _make_volume(conn, label, packs, location=None):
    """Create a volume, link packs, optionally add a copy at location."""
    vol = create_volume(conn, label, generate_uuid(), "TEST_TINY", 1_000_000,
                        status="VERIFIED")
    bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
    if location:
        add_volume_copy(conn, vol.volume_id, location)
    return vol


class TestLocationQueries:
    def test_packs_at_location_empty(self, conn):
        assert get_packs_at_location(conn, "Home_Shelf") == set()

    def test_packs_at_location(self, conn):
        p1 = register_pack(conn, "pack1", 100, "family")
        p2 = register_pack(conn, "pack2", 200, "family")
        _make_volume(conn, "V1", [p1, p2], "Home_Shelf")

        at_home = get_packs_at_location(conn, "Home_Shelf")
        assert at_home == {p1.pack_id, p2.pack_id}

    def test_packs_missing_at_location(self, conn):
        p1 = register_pack(conn, "pack1", 100, "family")
        p2 = register_pack(conn, "pack2", 200, "family")
        p3 = register_pack(conn, "pack3", 300, "work")

        # All 3 on volume at Home_Shelf
        _make_volume(conn, "V1", [p1, p2, p3], "Home_Shelf")
        # Only p1, p2 have a copy at Offsite_Safe
        _make_volume(conn, "V2", [p1, p2], "Offsite_Safe")

        missing = get_packs_missing_at_location(conn, "Offsite_Safe")
        assert len(missing) == 1
        assert missing[0].pack_id == p3.pack_id

    def test_unarchived_or_missing_at_location(self, conn):
        p1 = register_pack(conn, "pack1", 100, "family")
        p2 = register_pack(conn, "pack2", 200, "family")
        p3 = register_pack(conn, "pack3", 300, "work")  # not on any volume
        p4 = register_pack(conn, "pack4", 400, "work")  # on volume, no copy at offsite

        # p1, p2 on volume at both locations
        _make_volume(conn, "V1", [p1, p2], "Home_Shelf")
        add_volume_copy(conn,
                        create_volume(conn, "V1dup", generate_uuid(),
                                      "TEST_TINY", 1_000_000, status="VERIFIED").volume_id,
                        "Offsite_Safe")
        # Actually link p1, p2 to V1dup as well
        vol_dup = create_volume(conn, "V1dup2", generate_uuid(),
                                "TEST_TINY", 1_000_000, status="VERIFIED")
        bulk_link_packs(conn, vol_dup.volume_id, [p1.pack_id, p2.pack_id])
        add_volume_copy(conn, vol_dup.volume_id, "Offsite_Safe")

        # p4 on volume at Home_Shelf only
        _make_volume(conn, "V2", [p4], "Home_Shelf")

        # p3 not on any volume at all (globally unarchived)
        # p4 archived at Home_Shelf but missing at Offsite_Safe
        result = get_unarchived_or_missing_at_location(conn, "Offsite_Safe")
        result_ids = {p.pack_id for p in result}

        assert p3.pack_id in result_ids  # globally unarchived
        assert p4.pack_id in result_ids  # missing at Offsite_Safe
        assert p1.pack_id not in result_ids  # has copy at Offsite_Safe
        assert p2.pack_id not in result_ids  # has copy at Offsite_Safe

    def test_location_summary(self, conn):
        p1 = register_pack(conn, "pack1", 100, "family")
        p2 = register_pack(conn, "pack2", 200, "family")
        p3 = register_pack(conn, "pack3", 300, "work")

        _make_volume(conn, "V1", [p1, p2, p3], "Home_Shelf")
        _make_volume(conn, "V2", [p1, p2], "Offsite_Safe")

        summary = get_location_summary(conn)
        assert len(summary) == 2

        home = next(s for s in summary if s["location"] == "Home_Shelf")
        assert home["packs"] == 3
        assert home["missing"] == 0

        offsite = next(s for s in summary if s["location"] == "Offsite_Safe")
        assert offsite["packs"] == 2
        assert offsite["missing"] == 1

    def test_location_summary_empty(self, conn):
        summary = get_location_summary(conn)
        assert summary == []
