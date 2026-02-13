"""Tests for hashing, filesystem, and label utilities."""

from __future__ import annotations

from lcsas.utils.fs import (
    copy_file,
    copy_tree,
    dir_size_bytes,
    ensure_dir,
    hardlink_or_copy,
    list_files_recursive,
    safe_remove_tree,
)
from lcsas.utils.hashing import sha256_bytes, sha256_file
from lcsas.utils.labels import generate_uuid, generate_volume_label, next_seq_num


class TestHashing:
    def test_sha256_bytes_known(self):
        # SHA-256 of empty string
        assert sha256_bytes(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_sha256_bytes_hello(self):
        result = sha256_bytes(b"hello")
        assert len(result) == 64
        assert result == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    def test_sha256_file(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello")
        assert sha256_file(f) == sha256_bytes(b"hello")

    def test_sha256_file_large(self, tmp_path):
        """Test streaming hash with data larger than buffer."""
        f = tmp_path / "big.bin"
        data = b"x" * 200_000
        f.write_bytes(data)
        assert sha256_file(f) == sha256_bytes(data)


class TestFilesystem:
    def test_ensure_dir(self, tmp_path):
        d = tmp_path / "a" / "b" / "c"
        result = ensure_dir(d)
        assert result == d
        assert d.is_dir()

    def test_ensure_dir_idempotent(self, tmp_path):
        d = tmp_path / "existing"
        d.mkdir()
        ensure_dir(d)  # should not raise

    def test_hardlink_or_copy_same_fs(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("data")
        dst = tmp_path / "subdir" / "dst.txt"
        hardlink_or_copy(src, dst)
        assert dst.read_text() == "data"
        # Check it's a hardlink (same inode)
        assert src.stat().st_ino == dst.stat().st_ino

    def test_dir_size_bytes(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        (tmp_path / "b.txt").write_bytes(b"y" * 200)
        assert dir_size_bytes(tmp_path) == 300

    def test_dir_size_empty(self, tmp_path):
        assert dir_size_bytes(tmp_path) == 0

    def test_list_files_recursive(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("b")
        files = list_files_recursive(tmp_path)
        names = {f.name for f in files}
        assert names == {"a.txt", "b.txt"}

    def test_list_files_nonexistent(self, tmp_path):
        files = list_files_recursive(tmp_path / "nope")
        assert files == []

    def test_copy_tree(self, tmp_path):
        src = tmp_path / "src_tree"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        dst = tmp_path / "dst_tree"
        copy_tree(src, dst)
        assert (dst / "file.txt").read_text() == "hello"

    def test_copy_file(self, tmp_path):
        src = tmp_path / "orig.txt"
        src.write_text("content")
        dst = tmp_path / "deep" / "nested" / "copy.txt"
        copy_file(src, dst)
        assert dst.read_text() == "content"

    def test_safe_remove_tree(self, tmp_path):
        d = tmp_path / "removeme"
        d.mkdir()
        (d / "file.txt").write_text("bye")
        safe_remove_tree(d)
        assert not d.exists()

    def test_safe_remove_tree_nonexistent(self, tmp_path):
        safe_remove_tree(tmp_path / "nope")  # should not raise


class TestLabels:
    def test_generate_volume_label(self):
        label = generate_volume_label("LCSAS", "BD25", 1)
        assert label.startswith("LCSAS_")
        assert label.endswith("_001")

    def test_generate_uuid_format(self):
        uid = generate_uuid()
        assert len(uid) == 36
        assert uid.count("-") == 4

    def test_next_seq_num_empty(self):
        assert next_seq_num([], "LCSAS") == 1

    def test_next_seq_num_increments(self):
        labels = ["LCSAS_BD_2026_001", "LCSAS_BD_2026_003", "LCSAS_MD_2026_002"]
        assert next_seq_num(labels, "LCSAS") == 4

    def test_next_seq_num_ignores_other_prefix(self):
        labels = ["OTHER_BD_2026_005", "LCSAS_BD_2026_002"]
        assert next_seq_num(labels, "LCSAS") == 3
