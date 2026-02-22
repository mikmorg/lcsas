"""Tests for db/repos.py — repository CRUD."""

from __future__ import annotations

import sqlite3

import pytest

from lcsas.db.repos import delete_repo, get_repo, list_repos, register_repo
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
