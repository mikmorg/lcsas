"""Filesystem helper utilities."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
from pathlib import Path

_logger = logging.getLogger(__name__)


def ensure_dir(path: Path) -> Path:
    """Create a directory (and parents) if it doesn't exist. Returns the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """Create a hardlink from src to dst. Falls back to copy only on EXDEV.

    Creates parent directories of dst if needed.  Only cross-device link
    errors (EXDEV) trigger the copy fallback; all other OSErrors are
    re-raised so callers are not silently surprised by doubled disk usage
    or permission failures.
    """
    ensure_dir(dst.parent)
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            _logger.warning(
                "hardlink %s -> %s failed (errno %d: %s); will NOT fall back to copy",
                src, dst, exc.errno, exc.strerror,
            )
            raise
        # Cross-device link — fall back to copy (expected and safe)
        _logger.debug("hardlink cross-device, copying %s -> %s", src, dst)
        shutil.copy2(str(src), str(dst))


def dir_size_bytes(path: Path) -> int:
    """Calculate the total size in bytes of all files under a directory."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            with contextlib.suppress(OSError):
                total += os.path.getsize(fp)
    return total


def list_files_recursive(path: Path) -> list[Path]:
    """Recursively list all files under a directory (no directories)."""
    if not path.is_dir():
        return []
    return sorted(p for p in path.rglob("*") if p.is_file())


def copy_tree(src: Path, dst: Path) -> None:
    """Copy an entire directory tree. Overwrites dst if it exists.

    Uses a three-step sequence (copy → rename old → rename new) so that
    dst is never absent, even in the unlikely event of a crash mid-swap.

    Handles read-only source files/dirs (e.g. from rustic repos).
    """
    tmp_dst = dst.with_name(dst.name + ".copy_tmp")
    old_dst = dst.with_name(dst.name + ".copy_old")
    if tmp_dst.exists():
        _make_writable(tmp_dst)
        shutil.rmtree(tmp_dst)
    shutil.copytree(str(src), str(tmp_dst))
    # Swap: rename dst → .copy_old, then tmp → dst, then delete .copy_old
    if dst.exists():
        _make_writable(dst)
        dst.rename(old_dst)
    tmp_dst.rename(dst)
    if old_dst.exists():
        shutil.rmtree(old_dst)


def _make_writable(path: Path) -> None:
    """Recursively ensure all files and dirs under path are writable."""
    for dirpath, _dirnames, filenames in os.walk(path):
        dp = Path(dirpath)
        with contextlib.suppress(OSError):
            dp.chmod(dp.stat().st_mode | 0o700)
        for fname in filenames:
            fp = dp / fname
            with contextlib.suppress(OSError):
                if not fp.is_symlink():
                    fp.chmod(fp.stat().st_mode | 0o600)


def copy_file(src: Path, dst: Path) -> None:
    """Copy a single file, creating parent directories as needed.

    Removes any existing read-only destination file first.
    """
    ensure_dir(dst.parent)
    if dst.exists():
        dst.chmod(0o644)
        dst.unlink()
    shutil.copy2(str(src), str(dst))


def safe_remove_tree(path: Path) -> None:
    """Remove a directory tree if it exists. No error if missing.

    Handles read-only files/dirs by making them writable first.
    """
    if path.exists():
        _make_writable(path)
        shutil.rmtree(path)


def read_repo_key_ids(repo_path: Path) -> list[str]:
    """Read encryption key IDs from a rustic/restic repository.

    Key IDs are the filenames in the ``keys/`` subdirectory.

    Returns:
        Sorted list of key ID strings. Empty list if no keys found.
    """
    keys_dir = repo_path / "keys"
    if not keys_dir.is_dir():
        return []
    return sorted(f.name for f in keys_dir.iterdir() if f.is_file())
