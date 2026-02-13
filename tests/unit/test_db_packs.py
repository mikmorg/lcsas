"""Tests for packs CRUD operations."""

from __future__ import annotations

import pytest

from lcsas.db.packs import (
    bulk_register,
    get_pack_by_id,
    get_pack_by_sha256,
    list_packs,
    mark_pruned,
    register_pack,
)
from lcsas.db.repos import register_repo


class TestPacksCRUD:
    def test_register_and_fetch(self, memory_db):
        pack = register_pack(memory_db, sha256="abc123", size_bytes=1000)
        assert pack.sha256 == "abc123"
        assert pack.size_bytes == 1000
        assert pack.is_pruned is False

    def test_register_with_repo(self, memory_db):
        register_repo(memory_db, "r1", "Repo1", "/mirror/r1")
        pack = register_pack(memory_db, sha256="def456", size_bytes=2000, repo_id="r1")
        assert pack.repo_id == "r1"

    def test_register_duplicate_returns_existing(self, memory_db):
        p1 = register_pack(memory_db, sha256="dup_hash", size_bytes=500)
        p2 = register_pack(memory_db, sha256="dup_hash", size_bytes=999)
        assert p1.pack_id == p2.pack_id
        assert p2.size_bytes == 500  # original value preserved

    def test_get_by_sha256(self, memory_db):
        register_pack(memory_db, sha256="find_me", size_bytes=300)
        found = get_pack_by_sha256(memory_db, "find_me")
        assert found is not None
        assert found.sha256 == "find_me"

    def test_get_by_sha256_missing(self, memory_db):
        assert get_pack_by_sha256(memory_db, "nope") is None

    def test_get_by_id_not_found(self, memory_db):
        with pytest.raises(ValueError, match="not found"):
            get_pack_by_id(memory_db, 9999)

    def test_mark_pruned(self, memory_db):
        pack = register_pack(memory_db, sha256="prune_me", size_bytes=100)
        assert pack.is_pruned is False
        mark_pruned(memory_db, pack.pack_id)
        updated = get_pack_by_id(memory_db, pack.pack_id)
        assert updated.is_pruned is True

    def test_bulk_register(self, memory_db):
        data = [
            ("bulk_1", 100, None),
            ("bulk_2", 200, None),
            ("bulk_3", 300, None),
        ]
        packs = bulk_register(memory_db, data)
        assert len(packs) == 3
        assert packs[0].sha256 == "bulk_1"

    def test_list_packs_excludes_pruned_by_default(self, memory_db):
        register_pack(memory_db, sha256="active", size_bytes=100)
        p = register_pack(memory_db, sha256="dead", size_bytes=100)
        mark_pruned(memory_db, p.pack_id)

        visible = list_packs(memory_db)
        assert len(visible) == 1
        assert visible[0].sha256 == "active"

    def test_list_packs_include_pruned(self, memory_db):
        register_pack(memory_db, sha256="a1", size_bytes=100)
        p = register_pack(memory_db, sha256="a2", size_bytes=100)
        mark_pruned(memory_db, p.pack_id)

        all_packs = list_packs(memory_db, include_pruned=True)
        assert len(all_packs) == 2

    def test_list_packs_by_repo(self, memory_db):
        register_repo(memory_db, "rA", "A", "/a")
        register_repo(memory_db, "rB", "B", "/b")
        register_pack(memory_db, sha256="pA", size_bytes=100, repo_id="rA")
        register_pack(memory_db, sha256="pB", size_bytes=100, repo_id="rB")

        a_packs = list_packs(memory_db, repo_id="rA")
        assert len(a_packs) == 1
        assert a_packs[0].sha256 == "pA"
