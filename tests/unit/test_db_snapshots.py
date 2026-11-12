"""Tests for snapshots CRUD operations (lcsas.db.snapshots)."""

from __future__ import annotations

import sqlite3

from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all


def _mem_conn() -> sqlite3.Connection:
    conn = get_connection(":memory:")
    create_all(conn)
    return conn


class TestSnapshotDB:
    def test_upsert_and_list(self):
        from lcsas.db.snapshots import list_snapshots, upsert_snapshot

        conn = _mem_conn()
        register_repo(conn, "r1", "fam", "/mnt/fam", "")

        upsert_snapshot(conn, "snap1", "r1", "host1", "2024-01-01T00:00:00",
                        '["/" ]', '["daily"]', "desc")
        upsert_snapshot(conn, "snap2", "r1", "host1", "2024-01-02T00:00:00")

        snaps = list_snapshots(conn)
        assert len(snaps) == 2
        assert snaps[0].snapshot_id == "snap1"

    def test_upsert_replaces(self):
        from lcsas.db.snapshots import get_snapshot, upsert_snapshot

        conn = _mem_conn()
        register_repo(conn, "_test", "Test", "/test", "")
        upsert_snapshot(conn, "s1", "_test", "h1", "t1", description="v1")
        upsert_snapshot(conn, "s1", "_test", "h2", "t2", description="v2")

        snap = get_snapshot(conn, "s1")
        assert snap is not None
        assert snap.hostname == "h2"
        assert snap.description == "v2"

    def test_bulk_upsert(self):
        from lcsas.db.models import Snapshot
        from lcsas.db.snapshots import bulk_upsert_snapshots, list_snapshots

        conn = _mem_conn()
        register_repo(conn, "_test", "Test", "/test", "")
        snaps = [
            Snapshot("s1", "_test", "h1", "t1", "[]", "[]", ""),
            Snapshot("s2", "_test", "h2", "t2", "[]", "[]", ""),
            Snapshot("s3", "_test", "h3", "t3", "[]", "[]", ""),
        ]
        count = bulk_upsert_snapshots(conn, snaps)
        assert count == 3
        assert len(list_snapshots(conn)) == 3

    def test_list_by_repo(self):
        from lcsas.db.snapshots import list_snapshots, upsert_snapshot

        conn = _mem_conn()
        register_repo(conn, "r1", "a", "/a", "")
        register_repo(conn, "r2", "b", "/b", "")

        upsert_snapshot(conn, "s1", "r1", "h", "t1")
        upsert_snapshot(conn, "s2", "r2", "h", "t2")
        upsert_snapshot(conn, "s3", "r1", "h", "t3")

        assert len(list_snapshots(conn, repo_id="r1")) == 2
        assert len(list_snapshots(conn, repo_id="r2")) == 1

    def test_get_none(self):
        from lcsas.db.snapshots import get_snapshot

        conn = _mem_conn()
        assert get_snapshot(conn, "nonexistent") is None
