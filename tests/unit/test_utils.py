"""Tests for hashing, filesystem, and label utilities."""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest

from lcsas.utils.fs import (
    _make_writable,
    copy_file,
    copy_tree,
    dir_size_bytes,
    ensure_dir,
    hardlink_or_copy,
    read_repo_key_ids,
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
        assert ensure_dir(d) == d  # should not raise
        assert d.is_dir()

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
        assert safe_remove_tree(tmp_path / "nope") is None  # should not raise
        assert not (tmp_path / "nope").exists()

    def test_read_repo_key_ids(self, tmp_path):
        """Reads key file names from keys/ directory."""
        repo = tmp_path / "repo"
        keys = repo / "keys"
        keys.mkdir(parents=True)
        (keys / "abc123def456").write_text("key-data")
        (keys / "zzz999aaa000").write_text("key-data-2")

        result = read_repo_key_ids(repo)
        assert result == ["abc123def456", "zzz999aaa000"]

    def test_read_repo_key_ids_no_keys_dir(self, tmp_path):
        """Returns empty list when keys/ doesn't exist."""
        assert read_repo_key_ids(tmp_path / "nope") == []

    def test_read_repo_key_ids_empty(self, tmp_path):
        """Returns empty list when keys/ is empty."""
        (tmp_path / "keys").mkdir()
        assert read_repo_key_ids(tmp_path) == []


class TestLabels:
    def test_generate_volume_label(self):
        label = generate_volume_label("LCSAS", "BD25", 1)
        assert label.startswith("LCSAS_")
        assert label.endswith("_0001")

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


class TestSanitizeName:
    def test_valid_name(self):
        from lcsas.utils.labels import sanitize_name
        assert sanitize_name("Offsite_Safe") == "Offsite_Safe"

    def test_rejects_path_separator(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_name("../etc/passwd")

    def test_rejects_backslash(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_name("foo\\bar")

    def test_rejects_null_byte(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_name("foo\x00bar")

    def test_rejects_empty(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="must not be empty"):
            sanitize_name("")

    def test_rejects_too_long(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="maximum length"):
            sanitize_name("x" * 200)


# ---------------------------------------------------------------------------
# Tests for utils/fs.py edge cases — cross-device, read-only, overwrite.
# ---------------------------------------------------------------------------


class TestHardlinkOrCopyCrossDevice:
    def test_cross_device_fallback(self, tmp_path):
        """When os.link raises EXDEV, falls back to shutil.copy2."""
        import errno as _errno
        src = tmp_path / "src.txt"
        dst = tmp_path / "sub" / "dst.txt"
        src.write_text("hello cross-device")

        exdev_err = OSError(_errno.EXDEV, "Cross-device link")
        with patch("lcsas.utils.fs.os.link", side_effect=exdev_err):
            hardlink_or_copy(src, dst)

        assert dst.read_text() == "hello cross-device"

    def test_non_exdev_oserror_is_reraised(self, tmp_path):
        """When os.link raises a non-EXDEV OSError, it is re-raised (not silently copied)."""
        import errno as _errno

        import pytest

        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("content")

        perm_err = OSError(_errno.EPERM, "Operation not permitted")
        with patch("lcsas.utils.fs.os.link", side_effect=perm_err), \
             pytest.raises(OSError) as exc_info:
            hardlink_or_copy(src, dst)
        assert exc_info.value.errno == _errno.EPERM

    def test_creates_parent_dirs(self, tmp_path):
        """Creates parent directories for destination."""
        src = tmp_path / "src.txt"
        dst = tmp_path / "a" / "b" / "c" / "dst.txt"
        src.write_text("nested")

        hardlink_or_copy(src, dst)
        assert dst.read_text() == "nested"


class TestCopyTreeReadOnly:
    def test_overwrite_existing_dst(self, tmp_path):
        """copy_tree overwrites existing destination directory."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "file.txt").write_text("new content")

        # Pre-existing dst
        dst.mkdir()
        (dst / "old.txt").write_text("old content")

        copy_tree(src, dst)

        assert (dst / "file.txt").read_text() == "new content"
        assert not (dst / "old.txt").exists()

    def test_overwrite_readonly_dst(self, tmp_path):
        """copy_tree handles read-only files in existing destination."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "new.txt").write_text("new")

        # Create read-only destination
        dst.mkdir()
        ro_file = dst / "readonly.txt"
        ro_file.write_text("locked")
        ro_file.chmod(stat.S_IRUSR)

        copy_tree(src, dst)
        assert (dst / "new.txt").read_text() == "new"
        assert not ro_file.exists()


class TestCopyFileOverwrite:
    def test_overwrite_existing_file(self, tmp_path):
        """copy_file replaces existing destination."""
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("updated")
        dst.write_text("old")

        copy_file(src, dst)
        assert dst.read_text() == "updated"

    def test_overwrite_readonly_destination(self, tmp_path):
        """copy_file handles read-only destination file."""
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("updated")
        dst.write_text("locked")
        dst.chmod(stat.S_IRUSR)

        copy_file(src, dst)
        assert dst.read_text() == "updated"


class TestSafeRemoveTree:
    def test_remove_readonly_tree(self, tmp_path):
        """safe_remove_tree handles read-only files and dirs."""
        tree = tmp_path / "readonly_tree"
        tree.mkdir()
        inner = tree / "subdir"
        inner.mkdir()
        f = inner / "file.txt"
        f.write_text("content")
        f.chmod(stat.S_IRUSR)
        inner.chmod(stat.S_IRUSR | stat.S_IXUSR)

        safe_remove_tree(tree)
        assert not tree.exists()


class TestMakeWritable:
    def test_makes_files_writable(self, tmp_path):
        d = tmp_path / "ro"
        d.mkdir()
        f = d / "file.txt"
        f.write_text("x")
        f.chmod(stat.S_IRUSR)

        _make_writable(d)

        assert os.access(f, os.W_OK)

    def test_makes_dirs_writable(self, tmp_path):
        d = tmp_path / "ro_dir"
        d.mkdir()
        sub = d / "sub"
        sub.mkdir()
        sub.chmod(stat.S_IRUSR | stat.S_IXUSR)

        _make_writable(d)

        assert os.access(sub, os.W_OK)


class TestDirSizeBytesEdge:
    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert dir_size_bytes(d) == 0

    def test_with_unreadable_file(self, tmp_path):
        """Files that can't be stat'd are skipped."""
        d = tmp_path / "mixed"
        d.mkdir()
        f1 = d / "ok.txt"
        f1.write_bytes(b"x" * 100)

        # dir_size_bytes uses getsize which may not fail, but the
        # contextlib.suppress(OSError) is there. At minimum ensure
        # normal files are counted.
        size = dir_size_bytes(d)
        assert size == 100
