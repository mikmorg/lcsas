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

        Uses a single pass: each pack is located, validated, and hardlinked
        atomically before moving to the next one.  This eliminates the race
        window that exists in a two-pass approach where a pack could be
        deleted between verification and staging.

        Args:
            packs: List of Pack objects to stage.
            mirror_data_dir: Path to the mirror's data/ directory.

        Returns:
            Number of packs successfully staged.

        Raises:
            MissingPacksError: If any pack cannot be found, is a symlink, or
                its destination has zero size after staging.
        """
        ensure_dir(self._data_dir)

        missing: list[str] = []
        staged = 0
        total = len(packs)

        for i, pack in enumerate(packs, 1):
            short = pack.sha256[:12]

            # Locate the pack file immediately before using it.
            src = find_pack_file(mirror_data_dir, pack.sha256)
            if src is None:
                _logger.error("Pack %s not found in mirror (pack %d/%d)", short, i, total)
                missing.append(short)
                continue
            if src.is_symlink():
                _logger.error(
                    "Pack %s is a symlink — rejected (possible path injection): %s",
                    short, src,
                )
                missing.append(short)
                continue

            dst = pack_dest_path(self._data_dir, pack.sha256)
            ensure_dir(dst.parent)

            if dst.exists():
                # Verify existing staged file is not zero-byte or corrupt
                # (guards against partial stages from prior failed runs)
                dst_size = dst.stat().st_size if dst.exists() else 0
                if dst_size == 0:
                    _logger.warning(
                        "Pack %s was partially staged (zero-byte file). "
                        "Re-staging from source.",
                        short,
                    )
                    dst.unlink(missing_ok=True)
                    # Fall through to re-stage from source
                else:
                    # Existing file has content; assume it's valid
                    staged += 1
                    _logger.debug("Pack %s already staged, skipping (%d/%d)", short, i, total)
                    continue

            try:
                hardlink_or_copy(src, dst)
            except OSError as exc:
                _logger.error(
                    "Failed to stage pack %s (%s -> %s): %s",
                    short, src, dst, exc,
                )
                missing.append(short)
                continue

            # Verify the destination is non-empty (guards against silent failures).
            dst_size = dst.stat().st_size if dst.exists() else 0
            if dst_size == 0:
                _logger.error(
                    "Pack %s staged to %s but file is empty (expected %d bytes)",
                    short, dst, pack.size_bytes,
                )
                missing.append(short)
                dst.unlink(missing_ok=True)
                continue

            staged += 1
            if i % 100 == 0 or i == total:
                _logger.info("Staging packs: %d/%d complete", i, total)

        if missing:
            raise MissingPacksError(missing)

        return staged

    def _find_pack_file(self, data_dir: Path, sha256: str) -> Path | None:
        """Locate a pack file in the mirror data directory.

        Checks two-level hash-prefix layout first, then flat.
        """
        return find_pack_file(data_dir, sha256)

    def cleanup(self) -> None:
        """Remove the entire staging directory tree."""
        safe_remove_tree(self._root)
