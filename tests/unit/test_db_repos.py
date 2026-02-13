"""Tests for db/repos.py — repository CRUD."""

from __future__ import annotations

import sqlite3

import pytest

from lcsas.db.repos import delete_repo, get_repo, list_repos, register_repo


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
    def test_list_empty(self, memory_db):
        repos = list_repos(memory_db)
        assert repos == []

    def test_list_multiple(self, memory_db):
        register_repo(memory_db, "b", "Beta", "/b")
        register_repo(memory_db, "a", "Alpha", "/a")
        repos = list_repos(memory_db)
        assert len(repos) == 2
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
        assert repos == []
