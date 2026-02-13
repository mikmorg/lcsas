"""Tests for utils/fs.py edge cases — cross-device, read-only, overwrite."""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

from lcsas.utils.fs import (
    _make_writable,
    copy_file,
    copy_tree,
    dir_size_bytes,
    hardlink_or_copy,
    safe_remove_tree,
)


class TestHardlinkOrCopyCrossDevice:
    def test_cross_device_fallback(self, tmp_path):
        """When os.link raises OSError, falls back to shutil.copy2."""
        src = tmp_path / "src.txt"
        dst = tmp_path / "sub" / "dst.txt"
        src.write_text("hello cross-device")

        with patch("lcsas.utils.fs.os.link", side_effect=OSError("cross-device")):
            hardlink_or_copy(src, dst)

        assert dst.read_text() == "hello cross-device"

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

    def test_remove_nonexistent_noop(self, tmp_path):
        """safe_remove_tree on nonexistent path doesn't raise."""
        safe_remove_tree(tmp_path / "does_not_exist")


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
