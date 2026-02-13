"""Tests for database schema creation and versioning."""

from __future__ import annotations

from lcsas.db.connection import get_memory_connection
from lcsas.db.schema import CURRENT_SCHEMA_VERSION, create_all, get_schema_version


class TestSchema:
    def test_create_all_idempotent(self, memory_db):
        """Creating schema twice should not raise."""
        create_all(memory_db)  # already done in fixture
        create_all(memory_db)  # should be fine

    def test_schema_version_recorded(self, memory_db):
        version = get_schema_version(memory_db)
        assert version == CURRENT_SCHEMA_VERSION

    def test_tables_exist(self, memory_db):
        cursor = memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cursor.fetchall()}
        expected = {
            "schema_version", "volumes", "repositories",
            "packs", "volume_packs", "snapshots",
        }
        assert expected.issubset(tables)

    def test_foreign_keys_enabled(self, memory_db):
        row = memory_db.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_uninitialized_version(self):
        conn = get_memory_connection()
        assert get_schema_version(conn) == 0
        conn.close()
