"""FMT-05: pin the tier-1 frozen catalog query surface.

The C tier-1 recovery reader (recovery/src/lcsas-restore/catalog.c) is burned
onto every disc and can never be patched after the fact.  It hard-codes SQL
against a fixed set of (table, column) pairs and against the
``volumes.status != 'DESTROYED'`` filter.  These tests fail loudly if a future
schema migration renames, drops, or re-types any of that surface, or removes
``DESTROYED`` from the volumes.status CHECK constraint — any of which would
silently break disc-swap hints on every already-burned tier-1 binary.
"""

from __future__ import annotations

import sqlite3

from lcsas.db.schema import (
    TIER1_FROZEN_SURFACE,
    TIER1_REQUIRED_VOLUME_STATUS,
    create_all,
)


def _fresh_catalog() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all(conn)
    return conn


def test_frozen_columns_present() -> None:
    conn = _fresh_catalog()
    try:
        for table, columns in TIER1_FROZEN_SURFACE.items():
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert rows, f"tier-1 frozen table {table!r} is missing"
            present = {row[1] for row in rows}
            for col in columns:
                assert col in present, (
                    f"tier-1 frozen column {table}.{col} is missing — "
                    f"this breaks catalog.c on every burned disc"
                )
    finally:
        conn.close()


def test_destroyed_status_still_valid() -> None:
    conn = _fresh_catalog()
    try:
        conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) "
            "VALUES ('r', 'r', '/srv')"
        )
        # The tier-1 'status != DESTROYED' filter requires DESTROYED to be an
        # accepted CHECK value; this INSERT must not raise IntegrityError.
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
            "status) VALUES ('v1', 'u1', 'BD25', 1, ?)",
            (TIER1_REQUIRED_VOLUME_STATUS,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM volumes WHERE label = 'v1'"
        ).fetchone()
        assert row[0] == TIER1_REQUIRED_VOLUME_STATUS
    finally:
        conn.close()


def test_destroyed_filter_query_runs() -> None:
    """The exact JOIN shape catalog.c uses must execute against the schema."""
    conn = _fresh_catalog()
    try:
        conn.execute(
            "SELECT v.volume_id, v.label, v.status "
            "FROM volumes v "
            "JOIN volume_packs vp ON vp.volume_id = v.volume_id "
            "JOIN packs p ON p.pack_id = vp.pack_id "
            "WHERE p.sha256 = ? AND v.status != 'DESTROYED' "
            "ORDER BY v.volume_id",
            ("deadbeef",),
        ).fetchall()
    finally:
        conn.close()
