"""Tests for database schema creation and versioning."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.schema import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    create_all,
    ensure_schema,
    get_schema_version,
    migrate,
)

# Historical v5 catalog DDL, replayed verbatim (FMA-02).  This is the
# shape of a real catalog created at v4 and migrated 4→5: the v5
# migration recreated only `volumes` (adding CONSOLIDATING), so
# `volume_events` keeps the v4-era 6-type CHECK — no VERIFY_FAIL_REBURN,
# no BURN_RECEIPT_IMPORTED — and `session_volumes`/`volume_copies` lack
# the v7/v8 `iso_size_bytes` columns.
V5_HISTORICAL_DDL = """
CREATE TABLE schema_version (
    version INTEGER NOT NULL,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO schema_version (version) VALUES (4);
INSERT INTO schema_version (version) VALUES (5);
CREATE TABLE volumes (
    volume_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT UNIQUE NOT NULL,
    uuid        TEXT UNIQUE NOT NULL,
    media_type  TEXT NOT NULL,
    capacity_bytes INTEGER NOT NULL,
    used_bytes  INTEGER NOT NULL DEFAULT 0,
    location    TEXT NOT NULL DEFAULT 'Home_Shelf',
    status      TEXT NOT NULL DEFAULT 'STAGING'
                CHECK (status IN (
                    'STAGING', 'BURNING', 'BURNED',
                    'VERIFIED', 'CONSOLIDATING', 'DEPRECATED', 'DESTROYED'
                )),
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at   DATETIME,
    verified_at DATETIME
);
CREATE TABLE repositories (
    repo_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    mirror_path      TEXT NOT NULL,
    encryption_key_id TEXT NOT NULL DEFAULT '',
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE packs (
    pack_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256      TEXT UNIQUE NOT NULL,
    size_bytes  INTEGER NOT NULL,
    repo_id     TEXT NOT NULL,
    is_pruned   INTEGER NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories (repo_id)
);
CREATE TABLE volume_packs (
    volume_id   INTEGER NOT NULL,
    pack_id     INTEGER NOT NULL,
    PRIMARY KEY (volume_id, pack_id),
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
    FOREIGN KEY (pack_id)   REFERENCES packs (pack_id)
);
CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repo_id     TEXT NOT NULL,
    hostname    TEXT NOT NULL DEFAULT '',
    timestamp   DATETIME,
    paths       TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (repo_id) REFERENCES repositories (repo_id)
);
CREATE TABLE locations (
    name        TEXT PRIMARY KEY,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    description TEXT DEFAULT ''
);
CREATE TABLE volume_copies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id       INTEGER NOT NULL,
    location        TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'DEPRECATED', 'DESTROYED')),
    burn_date       TEXT    NOT NULL,
    notes           TEXT    DEFAULT '',
    iso_sha256      TEXT,
    last_verified_at DATETIME,
    media_serial    TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
    FOREIGN KEY (location) REFERENCES locations (name),
    UNIQUE(volume_id, location)
);
CREATE TABLE burn_sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    media_type  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'STAGED'
                CHECK (status IN ('STAGED', 'PARTIAL', 'COMPLETE', 'CLEANED')),
    staging_dir TEXT NOT NULL
);
CREATE TABLE session_volumes (
    session_id  TEXT    NOT NULL,
    volume_id   INTEGER NOT NULL,
    iso_path    TEXT    NOT NULL,
    iso_sha256  TEXT,
    PRIMARY KEY (session_id, volume_id),
    FOREIGN KEY (session_id) REFERENCES burn_sessions (session_id),
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id)
);
CREATE TABLE volume_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id   INTEGER NOT NULL,
    event_type  TEXT NOT NULL CHECK(event_type IN (
        'VERIFY_PASS', 'VERIFY_FAIL', 'ECC_REPAIR',
        'LOCATION_MOVE', 'CONDITION_CHECK', 'NOTE')),
    event_date  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    location    TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
    FOREIGN KEY (location)  REFERENCES locations (name)
);
"""


def seed_v5_catalog(conn: sqlite3.Connection) -> None:
    """Build a v5-shaped catalog by replaying the historical DDL."""
    conn.executescript(V5_HISTORICAL_DDL)
    conn.commit()


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


class TestMigrateV4ToV5:
    """Test the v4 → v5 migration (CONSOLIDATING status in volumes CHECK)."""

    def _make_v4_db(self):
        """Create a minimal v4 database with data in volume_copies and volume_events."""
        from lcsas.db.schema import SQL_CREATE_VOLUME_EVENTS
        conn = get_memory_connection()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")
        # v4 volumes table — no CONSOLIDATING in CHECK
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
        conn.executescript(SQL_CREATE_VOLUME_EVENTS)
        conn.commit()
        return conn

    def test_migrate_adds_consolidating_status(self):
        conn = self._make_v4_db()
        assert get_schema_version(conn) == 4
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION

        # CONSOLIDATING should now be a valid status
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES ('V1', 'uuid1', 'BD25', 25000000000, 'CONSOLIDATING')"
        )
        row = conn.execute("SELECT status FROM volumes WHERE label='V1'").fetchone()
        assert row[0] == "CONSOLIDATING"
        conn.close()

    def test_migrate_v4_preserves_existing_data(self):
        conn = self._make_v4_db()
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES ('EXISTING', 'uuid2', 'BD25', 25000000000, 'VERIFIED')"
        )
        conn.commit()
        migrate(conn)
        row = conn.execute("SELECT label, status FROM volumes WHERE label='EXISTING'").fetchone()
        assert row[0] == "EXISTING"
        assert row[1] == "VERIFIED"
        conn.close()

    def test_migrate_idempotent_from_v4(self):
        conn = self._make_v4_db()
        migrate(conn)
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        conn.close()

    def test_migrate_v4_to_v5_recreates_status_index(self):
        """Verify v5 migration recreates idx_volumes_status after table recreation."""
        conn = self._make_v4_db()
        # v4 DB might not have all indices; verify status index exists after migration
        migrate(conn)

        # Check that the status index exists
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert "idx_volumes_status" in indexes, (
            "idx_volumes_status not found after v4→v5 migration. "
            "Status-filtered queries will degrade to full table scans."
        )
        conn.close()


class TestMigrateV6ToV7:
    """v6 → v7: session_volumes.iso_size_bytes for device read-back
    verification (BURN-04)."""

    def _make_v6_db(self):
        """Minimal v6-era database: session_volumes without iso_size_bytes."""
        conn = get_memory_connection()
        conn.execute(
            """CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (6)")
        conn.execute(
            """CREATE TABLE session_volumes (
                session_id  TEXT    NOT NULL,
                volume_id   INTEGER NOT NULL,
                iso_path    TEXT    NOT NULL,
                iso_sha256  TEXT,
                PRIMARY KEY (session_id, volume_id)
            )"""
        )
        conn.execute(
            "INSERT INTO session_volumes (session_id, volume_id, iso_path, iso_sha256) "
            "VALUES ('s1', 1, '/staging/V1.iso', 'abc123')"
        )
        conn.commit()
        return conn

    def test_migration_v6_to_v7(self):
        conn = self._make_v6_db()
        assert get_schema_version(conn) == 6
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION

        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(session_volumes)").fetchall()
        }
        assert "iso_size_bytes" in cols

        # Pre-upgrade rows stay NULL (and keep their other columns)
        row = conn.execute(
            "SELECT iso_path, iso_sha256, iso_size_bytes FROM session_volumes"
        ).fetchone()
        assert row["iso_size_bytes"] is None
        assert row["iso_path"] == "/staging/V1.iso"
        assert row["iso_sha256"] == "abc123"
        conn.close()

    def test_migrate_idempotent_from_v6(self):
        conn = self._make_v6_db()
        migrate(conn)
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        conn.close()

    def test_create_all_applies_pending_migrations(self):
        """create_all on an existing old-version DB must migrate it:
        every CLI entry point calls create_all, none called migrate."""
        conn = self._make_v6_db()
        create_all(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(session_volumes)").fetchall()
        }
        assert "iso_size_bytes" in cols
        conn.close()


class TestMigrateV7ToV8:
    """v7 → v8: volume_copies.iso_size_bytes so device verification
    survives receipt import / catalog rebuild (FMA-03)."""

    def _make_v7_db(self):
        """Minimal v7-era database: volume_copies without iso_size_bytes."""
        conn = get_memory_connection()
        conn.execute(
            """CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (7)")
        conn.execute(
            """CREATE TABLE volume_copies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id       INTEGER NOT NULL,
                location        TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'ACTIVE',
                burn_date       TEXT    NOT NULL,
                notes           TEXT    DEFAULT '',
                iso_sha256      TEXT,
                last_verified_at DATETIME,
                media_serial    TEXT    NOT NULL DEFAULT '',
                UNIQUE(volume_id, location)
            )"""
        )
        conn.execute(
            "INSERT INTO volume_copies "
            "(volume_id, location, burn_date, iso_sha256) "
            "VALUES (1, 'Home_Shelf', '2026-01-01T00:00:00+00:00', 'abc123')"
        )
        conn.commit()
        return conn

    def test_migration_v7_to_v8(self):
        conn = self._make_v7_db()
        assert get_schema_version(conn) == 7
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION

        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(volume_copies)").fetchall()
        }
        assert "iso_size_bytes" in cols

        # Pre-upgrade rows stay NULL (and keep their other columns)
        row = conn.execute(
            "SELECT location, iso_sha256, iso_size_bytes FROM volume_copies"
        ).fetchone()
        assert row["iso_size_bytes"] is None
        assert row["location"] == "Home_Shelf"
        assert row["iso_sha256"] == "abc123"
        conn.close()

    def test_migrate_idempotent_from_v7(self):
        conn = self._make_v7_db()
        migrate(conn)
        migrate(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        conn.close()

    def test_fresh_create_all_has_column(self):
        conn = get_memory_connection()
        create_all(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(volume_copies)").fetchall()
        }
        assert "iso_size_bytes" in cols
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        conn.close()


class TestEnsureSchema:
    """FMA-02: ensure_schema is the single production schema entry point —
    auto-migrate hot catalogs, refuse future schemas, never write to
    read-only snapshots."""

    def test_cli_auto_migrates_old_catalog(self, tmp_path):
        """A v5 catalog opened by any CLI command is migrated in place."""
        from lcsas.cli.main import main

        db = tmp_path / "archive.db"
        conn = sqlite3.connect(db)
        seed_v5_catalog(conn)
        conn.close()

        assert main(["--db", str(db), "status"]) == 0

        conn = sqlite3.connect(db)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        # The recreated volume_events CHECK accepts the new event types.
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes) "
            "VALUES ('V1', 'uuid1', 'BD25', 25000000000)"
        )
        conn.execute(
            "INSERT INTO volume_events (volume_id, event_type) "
            "VALUES (1, 'VERIFY_FAIL_REBURN')"
        )
        conn.commit()
        conn.close()

    def test_refuses_future_schema(self):
        """Opening a catalog from a newer LCSAS must fail loud, not write."""
        conn = get_memory_connection()
        conn.execute(
            """CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (99)")
        conn.commit()

        with pytest.raises(SchemaVersionError, match="newer LCSAS"):
            ensure_schema(conn)

        # No write occurred: version untouched, no tables created.
        assert get_schema_version(conn) == 99
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "volumes" not in tables
        conn.close()

    def test_readonly_catalog_not_migrated(self, tmp_path, caplog):
        """Disc/RO-mount snapshots open in compat mode: warn, no write."""
        db = tmp_path / "disc-catalog.db"
        conn = sqlite3.connect(db)
        seed_v5_catalog(conn)
        conn.close()
        db.chmod(0o444)

        conn = sqlite3.connect(db)
        with caplog.at_level(logging.WARNING, logger="lcsas.db.schema"):
            assert ensure_schema(conn) == 5
        assert any("compat mode" in r.message for r in caplog.records)
        assert get_schema_version(conn) == 5
        conn.close()

    def test_query_only_connection_not_migrated(self):
        """PRAGMA query_only connections are detected as read-only too."""
        conn = sqlite3.connect(":memory:")
        seed_v5_catalog(conn)
        conn.execute("PRAGMA query_only=ON")
        assert ensure_schema(conn) == 5
        assert get_schema_version(conn) == 5
        conn.close()

    def test_no_bare_create_all_in_cli(self):
        """Production code must go through ensure_schema, never bare
        create_all — otherwise the future-version guard and the
        read-only compat path are silently bypassed."""
        repo_root = Path(__file__).resolve().parents[2]
        for rel in ("src/lcsas/cli/main.py", "src/lcsas/db/rebuild.py"):
            source = (repo_root / rel).read_text(encoding="utf-8")
            assert "create_all(" not in source, (
                f"{rel} calls create_all() directly — use "
                "lcsas.db.schema.ensure_schema instead (FMA-02)."
            )
