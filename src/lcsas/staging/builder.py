"""Staging directory builder for burn operations."""

from __future__ import annotations

import logging
from pathlib import Path

from lcsas.db.models import Pack
from lcsas.utils.fs import ensure_dir, hardlink_or_copy, safe_remove_tree
from lcsas.utils.pack_layout import find_pack_file, pack_dest_path

_logger = logging.getLogger(__name__)


class MissingPacksError(Exception):
    """Raised when one or more required packs are not found in the mirror."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            f"{len(missing)} pack(s) not found in mirror: "
            + ", ".join(missing[:5])
            + ("..." if len(missing) > 5 else "")
        )
        self.missing = missing


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

        # Pass 1: verify all packs are locatable before staging anything.
        # This prevents orphaned hardlinks if some packs are missing.
        missing: list[str] = []
        sources: dict[str, Path] = {}
        for pack in packs:
            src = find_pack_file(mirror_data_dir, pack.sha256)
            if src is None:
                missing.append(pack.sha256[:12])
            elif src.is_symlink():
                _logger.warning(
                    "Symlink pack file rejected (possible path injection): %s", src
                )
                missing.append(pack.sha256[:12])
            else:
                sources[pack.sha256] = src

        if missing:
            raise MissingPacksError(missing)

        # Pass 2: stage all packs (all are confirmed available).
        staged = 0
        for pack in packs:
            src = sources[pack.sha256]
            dst = pack_dest_path(self._data_dir, pack.sha256)
            ensure_dir(dst.parent)
            if not dst.exists():
                hardlink_or_copy(src, dst)
            staged += 1

        return staged

    def _find_pack_file(self, data_dir: Path, sha256: str) -> Path | None:
        """Locate a pack file in the mirror data directory.

        Checks two-level hash-prefix layout first, then flat.
        """
        return find_pack_file(data_dir, sha256)

    def cleanup(self) -> None:
        """Remove the entire staging directory tree."""
        safe_remove_tree(self._root)
