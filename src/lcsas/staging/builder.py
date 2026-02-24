"""Staging directory builder for burn operations."""

from __future__ import annotations

import logging
from pathlib import Path

from lcsas.db.models import Pack
from lcsas.utils.fs import ensure_dir, hardlink_or_copy, safe_remove_tree

_logger = logging.getLogger(__name__)


class StagingBuilder:
    """Assembles a staging directory tree ready for ISO mastering.

    The staging tree layout mirrors what will appear on the optical disc:
        staging_root/
        ├── data/                  # Pack files (hardlinked from mirror)
        ├── metadata/              # Holographic metadata (per-repo)
        │   └── <repo_id>/
        │       ├── index/
        │       ├── snapshots/
        │       ├── keys/
        │       └── config
        ├── catalog.db             # SQLite archive catalog
        └── volume_info.json       # Self-describing volume metadata
    """

    def __init__(self, staging_root: Path) -> None:
        self._root = staging_root
        self._data_dir = staging_root / "data"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def initialize(self) -> None:
        """Create the staging directory structure."""
        ensure_dir(self._root)
        ensure_dir(self._data_dir)

    def stage_packs(
        self,
        packs: list[Pack],
        mirror_data_dir: Path,
    ) -> int:
        """Hardlink selected packs from the mirror to the staging data dir.

        Handles both flat (data/HASH) and two-level (data/ab/abcdef...)
        mirror layouts by searching for the pack file.

        Args:
            packs: List of Pack objects to stage.
            mirror_data_dir: Path to the mirror's data/ directory.

        Returns:
            Number of packs successfully staged.
        """
        ensure_dir(self._data_dir)
        staged = 0
        missing: list[str] = []

        for pack in packs:
            src = self._find_pack_file(mirror_data_dir, pack.sha256)
            if src is None:
                missing.append(pack.sha256[:12])
                continue

            # Two-level layout: data/<prefix>/<hash>
            if len(pack.sha256) >= 2:
                prefix_dir = self._data_dir / pack.sha256[:2]
                ensure_dir(prefix_dir)
                dst = prefix_dir / pack.sha256
            else:
                dst = self._data_dir / pack.sha256

            if not dst.exists():
                hardlink_or_copy(src, dst)
            staged += 1

        if missing:
            _logger.warning(
                "stage_packs: %d pack(s) not found in %s: %s",
                len(missing), mirror_data_dir,
                ", ".join(missing[:5]) + ("..." if len(missing) > 5 else ""),
            )

        return staged

    def _find_pack_file(self, data_dir: Path, sha256: str) -> Path | None:
        """Locate a pack file in the mirror data directory.

        Checks two-level hash-prefix layout first, then flat.
        """
        # Two-level: data/ab/abcdef1234...
        if len(sha256) >= 2:
            prefixed = data_dir / sha256[:2] / sha256
            if prefixed.is_file():
                return prefixed

        # Flat: data/abcdef1234...
        flat = data_dir / sha256
        if flat.is_file():
            return flat

        return None

    def cleanup(self) -> None:
        """Remove the entire staging directory tree."""
        safe_remove_tree(self._root)
