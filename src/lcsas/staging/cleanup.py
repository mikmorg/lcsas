"""Detect and clean orphaned staging directories."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from lcsas.config.settings import LCSASConfig
from lcsas.utils.fs import safe_remove_tree

# Session directories use iso-timestamp with colons→dashes + UUID suffix,
# e.g. 2026-02-23T10-30-00.123456+00-00-a1b2c3d4
_SESSION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}")


def detect_orphaned_staging(
    config: LCSASConfig,
    conn: sqlite3.Connection,
) -> list[Path]:
    """Return staging directories that exist on disk but have no active session.

    A directory is considered orphaned if it is present under
    ``config.staging_path`` but is not referenced by any ``burn_sessions``
    row whose status is not ``'CLEANED'``.
    """
    staging_root = config.staging_path
    if not staging_root.is_dir():
        return []

    # Collect known active session staging dirs from the DB
    rows = conn.execute(
        "SELECT staging_dir FROM burn_sessions WHERE status != 'CLEANED'"
    ).fetchall()
    active_dirs = {Path(r[0]).resolve() for r in rows}

    orphans: list[Path] = []
    for child in sorted(staging_root.iterdir()):
        if not child.is_dir():
            continue
        # Only flag directories matching LCSAS session naming convention
        if not _SESSION_DIR_RE.match(child.name):
            continue
        if child.resolve() not in active_dirs:
            orphans.append(child)

    return orphans


def clean_orphaned_staging(paths: list[Path]) -> int:
    """Remove orphaned staging directories, returning the count removed."""
    removed = 0
    for p in paths:
        safe_remove_tree(p)
        removed += 1
    return removed
