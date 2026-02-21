"""Tests for database schema creation and versioning."""

from __future__ import annotations

from lcsas.db.connection import get_memory_connection
from lcsas.db.schema import CURRENT_SCHEMA_VERSION, create_all, get_schema_version, migrate


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

    def test_volume_events_table_exists(self, memory_db):
        """Schema v4 should include the volume_events table."""
        tables = {
            r["name"]
            for r in memory_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "volume_events" in tables

    def test_volume_copies_v4_columns(self, memory_db):
        """Schema v4 volume_copies should have iso_sha256, last_verified_at, media_serial."""
        cols = {
            r[1]
            for r in memory_db.execute("PRAGMA table_info(volume_copies)").fetchall()
        }
        assert "iso_sha256" in cols
        assert "last_verified_at" in cols
        assert "media_serial" in cols

    def test_foreign_keys_enabled(self, memory_db):
        row = memory_db.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_uninitialized_version(self):
        conn = get_memory_connection()
        assert get_schema_version(conn) == 0
        conn.close()


class TestMigrateV3ToV4:
    """Test the v3 → v4 migration path."""

    def _make_v3_db(self):
        """Create a minimal v3-era database (no volume_events, no extra volume_copies cols)."""
        conn = get_memory_connection()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        # volumes with verified_at (v3)
        conn.execute(
            """CREATE TABLE volumes (
                volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT UNIQUE NOT NULL,
                uuid TEXT UNIQUE NOT NULL,
                media_type TEXT NOT NULL,
                capacity_bytes INTEGER NOT NULL,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                location TEXT NOT NULL DEFAULT 'Home_Shelf',
                status TEXT NOT NULL DEFAULT 'STAGING',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                verified_at DATETIME
            )"""
        )
        conn.execute(
            """CREATE TABLE locations (
                name TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                description TEXT DEFAULT ''
            )"""
        )
        conn.execute(
            """CREATE TABLE repositories (
                repo_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mirror_path TEXT NOT NULL,
                encryption_key_id TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # volume_copies WITHOUT extra v4 columns
        conn.execute(
            """CREATE TABLE volume_copies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id INTEGER NOT NULL,
                location TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                burn_date TEXT NOT NULL,
                notes TEXT DEFAULT '',
                FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
                FOREIGN KEY (location) REFERENCES locations (name),
                UNIQUE(volume_id, location)
            )"""
        )
        conn.commit()
        return conn

    def test_migrate_creates_volume_events(self):
        conn = self._make_v3_db()
        assert get_schema_version(conn) == 3
        migrate(conn)
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "volume_events" in tables
        conn.close()

    def test_migrate_adds_volume_copies_columns(self):
        conn = self._make_v3_db()
        migrate(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(volume_copies)").fetchall()
        }
        assert "iso_sha256" in cols
        assert "last_verified_at" in cols
        assert "media_serial" in cols
        conn.close()

    def test_migrate_updates_version(self):
        conn = self._make_v3_db()
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        conn.close()

    def test_migrate_idempotent(self):
        """Running migrate twice should not raise."""
        conn = self._make_v3_db()
        migrate(conn)
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        conn.close()

    def test_migrate_preserves_existing_data(self):
        conn = self._make_v3_db()
        conn.execute("INSERT INTO locations (name) VALUES ('Home_Shelf')")
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes) "
            "VALUES ('V1', 'uuid1', 'BD25', 25000000000)"
        )
        conn.execute(
            "INSERT INTO volume_copies (volume_id, location, burn_date) "
            "VALUES (1, 'Home_Shelf', '2025-01-01')"
        )
        conn.commit()
        migrate(conn)
        row = conn.execute("SELECT * FROM volume_copies WHERE id = 1").fetchone()
        assert row["volume_id"] == 1
        assert row["location"] == "Home_Shelf"
        assert row["burn_date"] == "2025-01-01"
        # New columns should have defaults
        assert row["iso_sha256"] is None
        assert row["last_verified_at"] is None
        assert row["media_serial"] == ""
        conn.close()

    def test_volume_events_indexes_created(self):
        conn = self._make_v3_db()
        migrate(conn)
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_volume_events_volume" in indexes
        assert "idx_volume_events_type" in indexes
        conn.close()
