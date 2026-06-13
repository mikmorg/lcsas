"""Tests for db/key_escrow.py — the durable split record (KEY-08)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.key_escrow import (
    EscrowDriftError,
    clear_split,
    detect_escrow_drift,
    get_split,
    record_split,
)
from lcsas.db.schema import create_all, get_schema_version, migrate


@pytest.fixture
def conn():
    c = get_memory_connection()
    create_all(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_record_and_get(conn):
    rec = record_split(conn, "family", 3, 5, 4242)
    assert rec.repo_id == "family"
    assert (rec.threshold, rec.shares, rec.slip39_id) == (3, 5, 4242)
    assert rec.split_at  # ISO timestamp set

    got = get_split(conn, "family")
    assert got == rec


def test_get_missing_returns_none(conn):
    assert get_split(conn, "ghost") is None


def test_record_replace_on_rotation(conn):
    record_split(conn, "family", 2, 5, 111)
    record_split(conn, "family", 3, 7, 222)  # re-split (rotation)
    got = get_split(conn, "family")
    assert (got.threshold, got.shares, got.slip39_id) == (3, 7, 222)
    # Still a single row for the repo.
    rows = conn.execute(
        "SELECT COUNT(*) FROM key_escrow WHERE repo_id = 'family'"
    ).fetchone()[0]
    assert rows == 1


def test_clear_split(conn):
    record_split(conn, "family", 2, 5, 111)
    clear_split(conn, "family")
    assert get_split(conn, "family") is None
    clear_split(conn, "family")  # idempotent / no-op


def test_get_split_tolerates_missing_table():
    """v≤8 / pre-migration catalogs lack the table → get_split returns None."""
    c = get_memory_connection()
    # No create_all: the key_escrow table does not exist.
    assert get_split(c, "family") is None
    clear_split(c, "family")  # also a no-op, no crash
    c.close()


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_no_drift_single_key(conn):
    # No record, config says no split → consistent.
    assert detect_escrow_drift(
        conn, ["family"], config_split=False,
        config_threshold=2, config_shares=5,
    ) is None


def test_no_drift_matching_split(conn):
    record_split(conn, "family", 3, 5, 1)
    assert detect_escrow_drift(
        conn, ["family"], config_split=True,
        config_threshold=3, config_shares=5,
    ) is None


def test_drift_config_true_no_record(conn):
    msg = detect_escrow_drift(
        conn, ["family"], config_split=True,
        config_threshold=2, config_shares=5,
    )
    assert msg is not None
    assert "key_split=true" in msg and "no split" in msg


def test_drift_kn_mismatch(conn):
    record_split(conn, "family", 3, 5, 1)
    msg = detect_escrow_drift(
        conn, ["family"], config_split=True,
        config_threshold=2, config_shares=5,
    )
    assert msg is not None
    assert "2-of-5" in msg and "3-of-5" in msg


def test_drift_record_but_config_false(conn):
    record_split(conn, "family", 3, 5, 1)
    msg = detect_escrow_drift(
        conn, ["family"], config_split=False,
        config_threshold=2, config_shares=5,
    )
    assert msg is not None
    assert "key_split=false" in msg and "3-of-5" in msg


def test_escrow_drift_error_is_runtime_error():
    assert issubclass(EscrowDriftError, RuntimeError)


# ---------------------------------------------------------------------------
# Migration v8 → v9
# ---------------------------------------------------------------------------


def _v8_catalog():
    """A v8 catalog: full v9 schema minus key_escrow, version pinned to 8."""
    c = get_memory_connection()
    create_all(c)  # builds v9 schema
    # Roll back to a v8 shape: drop the v9-only table and rewrite the version.
    c.execute("DROP TABLE key_escrow")
    c.execute("DELETE FROM schema_version")
    c.execute("INSERT INTO schema_version (version) VALUES (8)")
    c.commit()
    return c


def test_migrate_v8_to_v9_adds_table():
    c = _v8_catalog()
    assert get_schema_version(c) == 8
    tables = {
        r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "key_escrow" not in tables

    migrate(c)

    assert get_schema_version(c) == 9
    tables = {
        r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "key_escrow" in tables
    # Usable after migration.
    record_split(c, "family", 2, 5, 7)
    assert get_split(c, "family").shares == 5
    c.close()


def test_migrate_v8_to_v9_preserves_existing_data():
    c = _v8_catalog()
    c.execute(
        "INSERT INTO repositories (repo_id, name, mirror_path) "
        "VALUES ('family', 'Family', '/mnt/mirror/family')"
    )
    c.commit()
    migrate(c)
    row = c.execute(
        "SELECT name FROM repositories WHERE repo_id = 'family'"
    ).fetchone()
    assert row["name"] == "Family"
    c.close()


# ---------------------------------------------------------------------------
# Rebuild from a disc catalog lacking key_escrow (KEY-08 compat note)
# ---------------------------------------------------------------------------


def test_rebuild_from_disc_catalog_without_key_escrow(tmp_path: Path):
    """A v≤8 disc catalog (no key_escrow) merges without error; the rebuilt
    catalog still gains the table from its own fresh schema."""
    from lcsas.db.connection import get_connection, locked_connection
    from lcsas.db.rebuild import rebuild_catalog

    # Build a mounted "disc" directory with a v8 catalog.db (no key_escrow).
    disc_dir = tmp_path / "disc1"
    disc_dir.mkdir()
    disc_db = disc_dir / "catalog.db"
    dc = get_connection(disc_db)
    create_all(dc)
    dc.execute("DROP TABLE key_escrow")
    dc.execute("DELETE FROM schema_version")
    dc.execute("INSERT INTO schema_version (version) VALUES (8)")
    dc.execute(
        "INSERT INTO repositories (repo_id, name, mirror_path) "
        "VALUES ('family', 'Family', '/mnt/mirror/family')"
    )
    dc.commit()
    dc.close()

    target_db = tmp_path / "rebuilt.db"
    result = rebuild_catalog([disc_dir], target_db)
    assert result.ok, result.errors
    assert result.repositories_merged == 1

    with locked_connection(target_db) as tc:
        tables = {
            r["name"] for r in tc.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "key_escrow" in tables  # rebuilt catalog has fresh v9 schema
        repo = tc.execute(
            "SELECT name FROM repositories WHERE repo_id = 'family'"
        ).fetchone()
        assert repo["name"] == "Family"
