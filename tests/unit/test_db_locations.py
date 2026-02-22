"""Tests for db/locations.py — location CRUD."""

from __future__ import annotations

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.locations import (
    create_location,
    delete_location,
    ensure_location,
    get_location,
    list_locations,
)
from lcsas.db.schema import create_all


@pytest.fixture
def conn():
    c = get_memory_connection()
    create_all(c)
    yield c
    c.close()


class TestLocationCRUD:
    def test_create_and_get(self, conn):
        loc = create_location(conn, "Home_Shelf", "Main bookshelf")
        assert loc.name == "Home_Shelf"
        assert loc.description == "Main bookshelf"

        fetched = get_location(conn, "Home_Shelf")
        assert fetched.name == loc.name

    def test_get_nonexistent_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            get_location(conn, "Nonexistent")

    def test_ensure_creates_if_missing(self, conn):
        loc = ensure_location(conn, "Bank_Vault", "Safe deposit box")
        assert loc.name == "Bank_Vault"

    def test_ensure_returns_existing(self, conn):
        create_location(conn, "Offsite", "Remote safe")
        loc = ensure_location(conn, "Offsite")
        assert loc.name == "Offsite"

    def test_list_locations(self, conn):
        create_location(conn, "A_Location")
        create_location(conn, "B_Location")
        locs = list_locations(conn)
        assert len(locs) == 2
        assert locs[0].name == "A_Location"

    def test_delete_location(self, conn):
        create_location(conn, "Temp")
        delete_location(conn, "Temp")
        assert list_locations(conn) == []

    def test_duplicate_raises(self, conn):
        import sqlite3 as _sqlite3
        create_location(conn, "Home_Shelf")
        with pytest.raises(_sqlite3.IntegrityError):
            create_location(conn, "Home_Shelf")
