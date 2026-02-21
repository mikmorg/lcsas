"""SQLite schema definitions for the LCSAS archive catalog."""

from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# DDL Statements
# ---------------------------------------------------------------------------

SQL_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

SQL_CREATE_VOLUMES = """
CREATE TABLE IF NOT EXISTS volumes (
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
                    'VERIFIED', 'DEPRECATED', 'DESTROYED'
                )),
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at   DATETIME,
    verified_at DATETIME
);
"""

SQL_CREATE_REPOSITORIES = """
CREATE TABLE IF NOT EXISTS repositories (
    repo_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    mirror_path      TEXT NOT NULL,
    encryption_key_id TEXT NOT NULL DEFAULT '',
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SQL_CREATE_PACKS = """
CREATE TABLE IF NOT EXISTS packs (
    pack_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256      TEXT UNIQUE NOT NULL,
    size_bytes  INTEGER NOT NULL,
    repo_id     TEXT NOT NULL,
    is_pruned   INTEGER NOT NULL DEFAULT 0,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories (repo_id)
);
"""

SQL_CREATE_VOLUME_PACKS = """
CREATE TABLE IF NOT EXISTS volume_packs (
    volume_id   INTEGER NOT NULL,
    pack_id     INTEGER NOT NULL,
    PRIMARY KEY (volume_id, pack_id),
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
    FOREIGN KEY (pack_id)   REFERENCES packs (pack_id)
);
"""

SQL_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repo_id     TEXT NOT NULL,
    hostname    TEXT NOT NULL DEFAULT '',
    timestamp   DATETIME,
    paths       TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (repo_id) REFERENCES repositories (repo_id)
);
"""

SQL_CREATE_LOCATIONS = """
CREATE TABLE IF NOT EXISTS locations (
    name        TEXT PRIMARY KEY,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    description TEXT DEFAULT ''
);
"""

SQL_CREATE_VOLUME_COPIES = """
CREATE TABLE IF NOT EXISTS volume_copies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id   INTEGER NOT NULL,
    location    TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'DEPRECATED', 'DESTROYED')),
    burn_date   TEXT    NOT NULL,
    notes       TEXT    DEFAULT '',
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
    FOREIGN KEY (location) REFERENCES locations (name),
    UNIQUE(volume_id, location)
);
"""

SQL_CREATE_BURN_SESSIONS = """
CREATE TABLE IF NOT EXISTS burn_sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    media_type  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'STAGED'
                CHECK (status IN ('STAGED', 'PARTIAL', 'COMPLETE', 'CLEANED')),
    staging_dir TEXT NOT NULL
);
"""

SQL_CREATE_SESSION_VOLUMES = """
CREATE TABLE IF NOT EXISTS session_volumes (
    session_id  TEXT    NOT NULL,
    volume_id   INTEGER NOT NULL,
    iso_path    TEXT    NOT NULL,
    iso_sha256  TEXT,
    PRIMARY KEY (session_id, volume_id),
    FOREIGN KEY (session_id) REFERENCES burn_sessions (session_id),
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id)
);
"""

# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------

SQL_CREATE_INDICES = [
    # packs.sha256 UNIQUE already creates an implicit index — no idx_packs_sha256 needed.
    "CREATE INDEX IF NOT EXISTS idx_packs_repo_id ON packs (repo_id);",
    "CREATE INDEX IF NOT EXISTS idx_packs_is_pruned ON packs (is_pruned);",
    # volume_packs PK (volume_id, pack_id) already indexes volume_id — only pack_id needs one.
    "CREATE INDEX IF NOT EXISTS idx_volume_packs_pack_id ON volume_packs (pack_id);",
    "CREATE INDEX IF NOT EXISTS idx_volumes_status ON volumes (status);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_repo_id ON snapshots (repo_id);",
    "CREATE INDEX IF NOT EXISTS idx_volume_copies_volume_id ON volume_copies (volume_id);",
    "CREATE INDEX IF NOT EXISTS idx_volume_copies_location ON volume_copies (location);",
    "CREATE INDEX IF NOT EXISTS idx_session_volumes_session ON session_volumes (session_id);",
]


def create_all(conn: sqlite3.Connection) -> None:
    """Create all tables and indices. Idempotent (IF NOT EXISTS)."""
    cursor = conn.cursor()

    cursor.execute(SQL_CREATE_SCHEMA_VERSION)
    cursor.execute(SQL_CREATE_VOLUMES)
    cursor.execute(SQL_CREATE_REPOSITORIES)
    cursor.execute(SQL_CREATE_PACKS)
    cursor.execute(SQL_CREATE_VOLUME_PACKS)
    cursor.execute(SQL_CREATE_SNAPSHOTS)
    cursor.execute(SQL_CREATE_LOCATIONS)
    cursor.execute(SQL_CREATE_VOLUME_COPIES)
    cursor.execute(SQL_CREATE_BURN_SESSIONS)
    cursor.execute(SQL_CREATE_SESSION_VOLUMES)

    for idx_sql in SQL_CREATE_INDICES:
        cursor.execute(idx_sql)

    # Record schema version (only if table is empty)
    cursor.execute("SELECT COUNT(*) FROM schema_version")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION,),
        )

    conn.commit()


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any pending schema migrations.  Returns the new version.

    This is safe to call on freshly-created databases (``create_all``
    already writes the latest version — nothing to migrate).  It is
    also safe to call on read-only catalog snapshots embedded on discs;
    ``ALTER TABLE … ADD COLUMN`` uses ``IF NOT EXISTS``-style safety
    via a column-existence check first.
    """
    current = get_schema_version(conn)
    if current >= CURRENT_SCHEMA_VERSION:
        return current

    cursor = conn.cursor()

    # v2 → v3: add verified_at column, created_at on repos
    if current < 3:
        # Add verified_at to volumes (if missing)
        cols = {r[1] for r in cursor.execute("PRAGMA table_info(volumes)").fetchall()}
        if "verified_at" not in cols:
            cursor.execute("ALTER TABLE volumes ADD COLUMN verified_at DATETIME")
        # Add created_at to repositories (if missing)
        cols = {r[1] for r in cursor.execute("PRAGMA table_info(repositories)").fetchall()}
        if "created_at" not in cols:
            cursor.execute(
                "ALTER TABLE repositories ADD COLUMN "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            )
        # Drop redundant indexes (safe if they don't exist)
        for idx in ("idx_packs_sha256", "idx_volume_packs_volume_id"):
            cursor.execute(f"DROP INDEX IF EXISTS {idx}")

        cursor.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (3,),
        )

    conn.commit()
    return CURRENT_SCHEMA_VERSION


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version, or 0 if uninitialized."""
    try:
        cursor = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0
