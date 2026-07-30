"""Tests for db/repos.py — repository CRUD."""

from __future__ import annotations

import sqlite3

import pytest

from lcsas.db.packs import register_pack
from lcsas.db.repos import (
    delete_repo,
    get_repo,
    list_repos,
    register_repo,
    set_repo_status,
)
from lcsas.db.snapshots import delete_snapshots_for_repo, upsert_snapshot


class TestRegisterRepo:
    def test_register_and_get(self, memory_db):
        repo = register_repo(memory_db, "family_1", "Family Photos", "/mnt/mirror/family")
        assert repo.repo_id == "family_1"
        assert repo.name == "Family Photos"
        assert repo.mirror_path == "/mnt/mirror/family"
        assert repo.encryption_key_id == ""

    def test_register_with_key(self, memory_db):
        repo = register_repo(
            memory_db, "work_1", "Work",
            "/mnt/mirror/work", encryption_key_id="key123"
        )
        assert repo.encryption_key_id == "key123"

    def test_duplicate_repo_id_raises(self, memory_db):
        register_repo(memory_db, "dup", "First", "/path1")
        with pytest.raises(sqlite3.IntegrityError):
            register_repo(memory_db, "dup", "Second", "/path2")


class TestGetRepo:
    def test_get_existing(self, memory_db):
        register_repo(memory_db, "r1", "Repo One", "/mnt/r1")
        repo = get_repo(memory_db, "r1")
        assert repo.name == "Repo One"

    def test_get_nonexistent_raises(self, memory_db):
        with pytest.raises(ValueError, match="not found"):
            get_repo(memory_db, "nonexistent_repo")


class TestListRepos:
    def test_list_includes_default(self, memory_db):
        repos = list_repos(memory_db)
        assert len(repos) == 1
        assert repos[0].repo_id == "_test"

    def test_list_multiple(self, memory_db):
        register_repo(memory_db, "b", "Beta", "/b")
        register_repo(memory_db, "a", "Alpha", "/a")
        repos = list_repos(memory_db)
        assert len(repos) == 3
        # Should be ordered by name
        assert repos[0].name == "Alpha"
        assert repos[1].name == "Beta"


class TestDeleteRepo:
    def test_delete_existing(self, memory_db):
        register_repo(memory_db, "del_me", "To Delete", "/del")
        delete_repo(memory_db, "del_me")
        with pytest.raises(ValueError, match="not found"):
            get_repo(memory_db, "del_me")

    def test_delete_nonexistent_noop(self, memory_db):
        """Deleting non-existent repo doesn't raise."""
        delete_repo(memory_db, "doesnt_exist")
        repos = list_repos(memory_db)
        # Only the fixture's _test repo should remain
        assert len(repos) == 1
        assert repos[0].repo_id == "_test"

    def test_delete_repo_with_packs_raises(self, memory_db):
        """Deleting a repo that owns packs raises ValueError."""
        register_repo(memory_db, "has_packs", "Has Packs", "/packs")
        register_pack(memory_db, sha256="a" * 64, size_bytes=100, repo_id="has_packs")
        with pytest.raises(ValueError, match="associated pack"):
            delete_repo(memory_db, "has_packs")


class TestDeleteSnapshotsForRepo:
    def test_deletes_matching_snapshots(self, memory_db):
        register_repo(memory_db, "snap_repo", "Snap Repo", "/snap")
        upsert_snapshot(memory_db, snapshot_id="s1", repo_id="snap_repo",
                        hostname="h", timestamp="2026-01-01T00:00:00Z",
                        paths="/data", tags="", description="")
        upsert_snapshot(memory_db, snapshot_id="s2", repo_id="snap_repo",
                        hostname="h", timestamp="2026-01-02T00:00:00Z",
                        paths="/data", tags="", description="")
        count = delete_snapshots_for_repo(memory_db, "snap_repo")
        assert count == 2

    def test_does_not_delete_other_repos(self, memory_db):
        register_repo(memory_db, "r_a", "A", "/a")
        register_repo(memory_db, "r_b", "B", "/b")
        upsert_snapshot(memory_db, snapshot_id="sa", repo_id="r_a",
                        hostname="h", timestamp="2026-01-01T00:00:00Z",
                        paths="/a", tags="", description="")
        upsert_snapshot(memory_db, snapshot_id="sb", repo_id="r_b",
                        hostname="h", timestamp="2026-01-01T00:00:00Z",
                        paths="/b", tags="", description="")
        count = delete_snapshots_for_repo(memory_db, "r_a")
        assert count == 1
        from lcsas.db.snapshots import get_snapshot
        assert get_snapshot(memory_db, "sb") is not None


class TestRepoStatus:
    """Schema v10: repositories.status — 'active' by default, flipped to
    'retired' when a mirror is gone for good (#437)."""

    def test_new_repo_is_active(self, memory_db):
        repo = register_repo(memory_db, "r_new", "New", "/mnt/new")
        assert repo.status == "active"

    def test_retire_then_activate_round_trip(self, memory_db):
        register_repo(memory_db, "r_rt", "Round Trip", "/mnt/rt")

        retired = set_repo_status(memory_db, "r_rt", "retired")
        assert retired.status == "retired"
        assert get_repo(memory_db, "r_rt").status == "retired"

        active = set_repo_status(memory_db, "r_rt", "active")
        assert active.status == "active"
        assert get_repo(memory_db, "r_rt").status == "active"

    def test_retiring_preserves_the_rest_of_the_row(self, memory_db):
        """Retirement is a flag, not a deletion — `repo remove --force`
        is the destructive verb, this one keeps pack history reachable."""
        register_repo(
            memory_db, "r_keep", "Keep", "/mnt/keep", encryption_key_id="k9"
        )
        register_pack(memory_db, sha256="c" * 64, size_bytes=42, repo_id="r_keep")

        set_repo_status(memory_db, "r_keep", "retired")

        repo = get_repo(memory_db, "r_keep")
        assert repo.name == "Keep"
        assert repo.mirror_path == "/mnt/keep"
        assert repo.encryption_key_id == "k9"
        n_packs = memory_db.execute(
            "SELECT COUNT(*) FROM packs WHERE repo_id = 'r_keep'"
        ).fetchone()[0]
        assert n_packs == 1

    def test_invalid_status_raises(self, memory_db):
        register_repo(memory_db, "r_bad", "Bad", "/mnt/bad")
        with pytest.raises(ValueError, match="Invalid repository status"):
            set_repo_status(memory_db, "r_bad", "deleted")
        # ...and the row is untouched
        assert get_repo(memory_db, "r_bad").status == "active"

    def test_unknown_repo_raises(self, memory_db):
        with pytest.raises(ValueError, match="not found"):
            set_repo_status(memory_db, "nope", "retired")

    def test_only_the_named_repo_changes(self, memory_db):
        register_repo(memory_db, "r_x", "X", "/x")
        register_repo(memory_db, "r_y", "Y", "/y")
        set_repo_status(memory_db, "r_x", "retired")
        assert get_repo(memory_db, "r_y").status == "active"

    def test_v9_catalog_row_reads_back_active(self):
        """A pre-v10 catalog has no `status` column at all — on-disc
        holographic catalogs are never migrated in place, so the row→model
        mapping must default rather than raise."""
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(":memory:")
        conn.row_factory = _sqlite3.Row
        conn.execute(
            """CREATE TABLE repositories (
                repo_id          TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                mirror_path      TEXT NOT NULL,
                encryption_key_id TEXT NOT NULL DEFAULT '',
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) "
            "VALUES ('old', 'Old', '/mnt/old')"
        )
        conn.commit()

        repo = get_repo(conn, "old")
        assert repo.status == "active"
        assert [r.status for r in list_repos(conn)] == ["active"]
        conn.close()
