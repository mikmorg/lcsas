"""SQLite schema definitions for the LCSAS archive catalog."""

from __future__ import annotations

import logging
import sqlite3

_logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 8


class SchemaVersionError(RuntimeError):
    """Catalog schema is newer than this LCSAS build understands."""


class WedgedMigrationError(RuntimeError):
    """Catalog holds ``*_old`` leftovers from an interrupted migration."""

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
                    'VERIFIED', 'CONSOLIDATING', 'DEPRECATED', 'DESTROYED'
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
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id) ON DELETE CASCADE,
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id       INTEGER NOT NULL,
    location        TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'DEPRECATED', 'DESTROYED')),
    burn_date       TEXT    NOT NULL,
    notes           TEXT    DEFAULT '',
    iso_sha256      TEXT,
    iso_size_bytes  INTEGER,
    last_verified_at DATETIME,
    media_serial    TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id) ON DELETE CASCADE,
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
    iso_size_bytes INTEGER,
    PRIMARY KEY (session_id, volume_id),
    FOREIGN KEY (session_id) REFERENCES burn_sessions (session_id),
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id)
);
"""

SQL_CREATE_VOLUME_EVENTS = """
CREATE TABLE IF NOT EXISTS volume_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id   INTEGER NOT NULL,
    event_type  TEXT NOT NULL CHECK(event_type IN (
        'VERIFY_PASS', 'VERIFY_FAIL', 'VERIFY_FAIL_REBURN', 'ECC_REPAIR',
        'LOCATION_MOVE', 'CONDITION_CHECK', 'NOTE',
        'BURN_RECEIPT_IMPORTED')),
    event_date  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    location    TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id) ON DELETE CASCADE,
    FOREIGN KEY (location)  REFERENCES locations (name)
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
    "CREATE INDEX IF NOT EXISTS idx_volume_events_volume ON volume_events (volume_id);",
    "CREATE INDEX IF NOT EXISTS idx_volume_events_type ON volume_events (event_type);",
    "CREATE INDEX IF NOT EXISTS idx_volume_events_date ON volume_events (event_date);",
]


def detect_wedged_migration(conn: sqlite3.Connection) -> list[str]:
    """Return leftover ``*_old`` table names from an interrupted migration.

    The table-recreating migrations (v4→v5 on ``volumes``, v5→v6 on
    ``volume_events``) rename the live table to ``<name>_old`` while
    rebuilding it.  Post-FMA-07 the whole rebuild is one transaction, so
    a leftover can only come from a pre-fix crash — and the original
    rows are intact inside the ``*_old`` table.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name LIKE '%\\_old' ESCAPE '\\' "
        "ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _refuse_wedged(conn: sqlite3.Connection) -> None:
    """Raise if the catalog was wedged by an interrupted migration."""
    leftovers = detect_wedged_migration(conn)
    if leftovers:
        names = ", ".join(leftovers)
        raise WedgedMigrationError(
            f"Catalog contains leftover table(s) {names} from an "
            "interrupted schema migration. Do NOT continue. Restore the "
            "catalog from backup, or recover manually: the original data "
            f"is intact in {names}. "
            "See docs/RUNBOOK_migration_recovery.md."
        )


def create_all(conn: sqlite3.Connection) -> None:
    """Create all tables and indices. Idempotent (IF NOT EXISTS).

    Refuses wedged catalogs (leftover ``*_old`` tables) before creating
    anything, so an interrupted pre-FMA-07 migration is never masked by
    recreating empty shadow tables over the renamed originals.
    """
    _refuse_wedged(conn)
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
    cursor.execute(SQL_CREATE_VOLUME_EVENTS)

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

    # Apply pending migrations: production reaches create_all only via
    # ensure_schema (FMA-02), but keep migrate() here too so any direct
    # caller still upgrades long-lived catalogs (BURN-04 needs
    # session_volumes.iso_size_bytes on upgraded v≤6 databases).
    # No-op on freshly created databases (version is already current).
    migrate(conn)


def _is_readonly(conn: sqlite3.Connection) -> bool:
    """True when *conn* cannot write the main database.

    Catches ``PRAGMA query_only`` connections, ``mode=ro`` URI opens,
    and read-only files/mounts (disc catalogs live on ISO9660).  A
    ``BEGIN IMMEDIATE`` alone is not enough — SQLite defers the actual
    write attempt — so a zero-row UPDATE forces the readonly error.
    """
    row = conn.execute("PRAGMA query_only").fetchone()
    if row is not None and row[0]:
        return True
    if conn.in_transaction:
        # An open transaction proves a writable connection (and BEGIN
        # would fail with "within a transaction" — not a readonly signal).
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return True
    try:
        conn.execute("UPDATE schema_version SET version = version WHERE 0")
    except sqlite3.OperationalError as exc:
        # "no such table" on an uninitialized DB is not a readonly signal.
        return "readonly" in str(exc).lower()
    finally:
        conn.execute("ROLLBACK")
    return False


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Create-or-migrate the catalog schema; refuse future schemas.

    The ONLY schema entry point production code should call (FMA-02).
    Returns the catalog's schema version after the call:
    ``CURRENT_SCHEMA_VERSION`` on writable catalogs (created or migrated
    in place), or the existing older version on read-only snapshots —
    on-disc holographic catalogs are opened from read-only ISO9660
    mounts and must never be migrated in place; reader code tolerates
    old shapes (compat mode).
    """
    version = get_schema_version(conn)
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Catalog schema is v{version}, but this LCSAS build understands "
            f"up to v{CURRENT_SCHEMA_VERSION}. Use a newer LCSAS to open this "
            f"catalog (writing with this build could corrupt it)."
        )
    if _is_readonly(conn):
        if version < CURRENT_SCHEMA_VERSION:
            _logger.warning(
                "Catalog is v%d (read-only) — running in compat mode "
                "without migrating to v%d.",
                version, CURRENT_SCHEMA_VERSION,
            )
        return version
    create_all(conn)  # runs migrate() on the hot catalog
    return CURRENT_SCHEMA_VERSION


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any pending schema migrations.  Returns the new version.

    This is safe to call on freshly-created databases (``create_all``
    already writes the latest version — nothing to migrate).  It is
    also safe to call on read-only catalog snapshots embedded on discs;
    ``ALTER TABLE … ADD COLUMN`` uses ``IF NOT EXISTS``-style safety
    via a column-existence check first.

    Crash-atomicity (FMA-07): the connection is switched to true
    autocommit (``isolation_level = None``) so Python's legacy DML
    handling can never pre-commit mid-step, and every version step runs
    inside an explicit ``BEGIN IMMEDIATE`` … ``COMMIT``.  SQLite DDL is
    fully transactional, so a crash anywhere inside a step rolls the
    whole step back to a consistent pre-step catalog.  New migrations
    MUST follow the same template.  ``executescript`` is forbidden here
    (it implicitly commits any open transaction).
    """
    _refuse_wedged(conn)
    current = get_schema_version(conn)
    if current >= CURRENT_SCHEMA_VERSION:
        return current

    prior_isolation = conn.isolation_level
    conn.isolation_level = None  # commits any pending transaction
    cursor = conn.cursor()
    fk_off = False
    try:
        # v2 → v3: add verified_at column, created_at on repos
        if current < 3:
            cursor.execute("BEGIN IMMEDIATE")
            cols = {r[1] for r in cursor.execute("PRAGMA table_info(volumes)").fetchall()}
            if "verified_at" not in cols:
                cursor.execute("ALTER TABLE volumes ADD COLUMN verified_at DATETIME")
            cols = {r[1] for r in cursor.execute("PRAGMA table_info(repositories)").fetchall()}
            if "created_at" not in cols:
                cursor.execute(
                    "ALTER TABLE repositories ADD COLUMN "
                    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                )
            cursor.execute("DROP INDEX IF EXISTS idx_packs_sha256")
            cursor.execute("DROP INDEX IF EXISTS idx_volume_packs_volume_id")
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (3,),
            )
            cursor.execute("COMMIT")

        # v3 → v4: volume_events table, volume_copies extra columns
        if current < 4:
            cursor.execute("BEGIN IMMEDIATE")
            # Create volume_events table (idempotent)
            cursor.execute(SQL_CREATE_VOLUME_EVENTS)
            for idx_sql in (
                "CREATE INDEX IF NOT EXISTS idx_volume_events_volume "
                "ON volume_events (volume_id);",
                "CREATE INDEX IF NOT EXISTS idx_volume_events_type "
                "ON volume_events (event_type);",
            ):
                cursor.execute(idx_sql)

            # Add columns to volume_copies (if missing)
            cols = {r[1] for r in cursor.execute("PRAGMA table_info(volume_copies)").fetchall()}
            if "iso_sha256" not in cols:
                cursor.execute("ALTER TABLE volume_copies ADD COLUMN iso_sha256 TEXT")
            if "last_verified_at" not in cols:
                cursor.execute("ALTER TABLE volume_copies ADD COLUMN last_verified_at DATETIME")
            if "media_serial" not in cols:
                cursor.execute(
                    "ALTER TABLE volume_copies ADD COLUMN media_serial TEXT NOT NULL DEFAULT ''"
                )

            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (4,),
            )
            cursor.execute("COMMIT")

        # v4 → v5: widen volumes.status CHECK to include CONSOLIDATING.
        # SQLite cannot alter CHECK constraints — must recreate the table.
        # We disable FK enforcement during the swap to avoid spurious violations.
        # PRAGMA foreign_keys is a silent no-op inside a transaction, so it
        # must be set before BEGIN (we are in autocommit here).
        if current < 5:
            conn.execute("PRAGMA foreign_keys=OFF")
            fk_off = True
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("ALTER TABLE volumes RENAME TO volumes_old")
            cursor.execute(
                """CREATE TABLE volumes (
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
                )"""
            )
            cursor.execute("INSERT INTO volumes SELECT * FROM volumes_old")
            cursor.execute("DROP TABLE volumes_old")
            # Recreate the status index (v5 migration dropped the old one)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_volumes_status ON volumes (status);"
            )
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (5,),
            )
            cursor.execute("COMMIT")
            conn.execute("PRAGMA foreign_keys=ON")
            fk_off = False

        # v5 → v6: widen volume_events.event_type CHECK to include
        # 'BURN_RECEIPT_IMPORTED'. SQLite cannot alter CHECK constraints —
        # the table must be recreated. No data shape changes.
        if current < 6:
            conn.execute("PRAGMA foreign_keys=OFF")
            fk_off = True
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("ALTER TABLE volume_events RENAME TO volume_events_old")
            cursor.execute(SQL_CREATE_VOLUME_EVENTS)
            cursor.execute(
                "INSERT INTO volume_events "
                "(event_id, volume_id, event_type, event_date, location, detail) "
                "SELECT event_id, volume_id, event_type, event_date, location, detail "
                "FROM volume_events_old"
            )
            cursor.execute("DROP TABLE volume_events_old")
            for idx_sql in (
                "CREATE INDEX IF NOT EXISTS idx_volume_events_volume "
                "ON volume_events (volume_id);",
                "CREATE INDEX IF NOT EXISTS idx_volume_events_type "
                "ON volume_events (event_type);",
                "CREATE INDEX IF NOT EXISTS idx_volume_events_date "
                "ON volume_events (event_date);",
            ):
                cursor.execute(idx_sql)
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (6,),
            )
            cursor.execute("COMMIT")
            conn.execute("PRAGMA foreign_keys=ON")
            fk_off = False

        # v6 → v7: session_volumes.iso_size_bytes — the post-ECC ISO byte
        # length, required to device-hash exactly the burned image after the
        # ISO file itself has been cleaned up (BURN-04).  Nullable single
        # ALTER, mirroring the v3→v4 pattern; pre-upgrade rows stay NULL.
        if current < 7:
            cursor.execute("BEGIN IMMEDIATE")
            tables = {
                r[0] for r in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "session_volumes" not in tables:
                # Partial legacy catalogs (pre-session era): create fresh
                # with the column already present.
                cursor.execute(SQL_CREATE_SESSION_VOLUMES)
            else:
                cols = {
                    r[1] for r in cursor.execute(
                        "PRAGMA table_info(session_volumes)"
                    ).fetchall()
                }
                if "iso_size_bytes" not in cols:
                    cursor.execute(
                        "ALTER TABLE session_volumes ADD COLUMN iso_size_bytes INTEGER"
                    )
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (7,),
            )
            cursor.execute("COMMIT")

        # v7 → v8: volume_copies.iso_size_bytes — the same post-ECC byte
        # length on the per-location copy row, so device verification still
        # has a length after a receipt import or a catalog rebuild from
        # disc copies (session_volumes is not merged by rebuild) (FMA-03).
        if current < 8:
            cursor.execute("BEGIN IMMEDIATE")
            tables = {
                r[0] for r in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "volume_copies" not in tables:
                cursor.execute(SQL_CREATE_VOLUME_COPIES)
            else:
                cols = {
                    r[1] for r in cursor.execute(
                        "PRAGMA table_info(volume_copies)"
                    ).fetchall()
                }
                if "iso_size_bytes" not in cols:
                    cursor.execute(
                        "ALTER TABLE volume_copies ADD COLUMN iso_size_bytes INTEGER"
                    )
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (8,),
            )
            cursor.execute("COMMIT")
    except BaseException:
        # A failed step must leave the catalog exactly as it was before
        # the step: roll back the open block and restore FK enforcement.
        if conn.in_transaction:
            conn.rollback()
        if fk_off:
            conn.execute("PRAGMA foreign_keys=ON")
        raise
    finally:
        conn.isolation_level = prior_isolation

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
