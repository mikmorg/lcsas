"""Filesystem helper utilities."""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Create a directory (and parents) if it doesn't exist. Returns the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """Create a hardlink from src to dst. Falls back to copy if cross-device.

    Creates parent directories of dst if needed.
    """
    ensure_dir(dst.parent)
    try:
        os.link(src, dst)
    except OSError:
        # Cross-device link — fall back to copy
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

    Handles read-only source files/dirs (e.g. from restic repos).
    """
    if dst.exists():
        _make_writable(dst)
        shutil.rmtree(dst)
    shutil.copytree(str(src), str(dst))


def _make_writable(path: Path) -> None:
    """Recursively ensure all files and dirs under path are writable."""
    for dirpath, _dirnames, filenames in os.walk(path):
        dp = Path(dirpath)
        dp.chmod(dp.stat().st_mode | 0o700)
        for fname in filenames:
            fp = dp / fname
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
