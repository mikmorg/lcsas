"""Staging directory builder for burn operations."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from lcsas.db.models import Pack
from lcsas.utils.fs import ensure_dir, hardlink_or_copy, safe_remove_tree
from lcsas.utils.hashing import sha256_file
from lcsas.utils.pack_layout import find_pack_file, pack_dest_path

_logger = logging.getLogger(__name__)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class MissingPacksError(Exception):
    """Raised when one or more required packs are not found in the mirror."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            f"{len(missing)} pack(s) not found in mirror: "
            + ", ".join(missing[:5])
            + ("..." if len(missing) > 5 else "")
        )
        self.missing = missing


class MirrorUnavailableError(MissingPacksError):
    """A repo's mirror (or its ``data/`` dir) is absent while packs from it
    were selected for staging.

    Subclasses :class:`MissingPacksError` so existing handlers keep working,
    but carries its own user-facing message.
    """

    def __init__(
        self,
        repo_id: str,
        mirror_path: Path | None,
        packs: list[str],
        detail: str | None = None,
    ) -> None:
        where = (
            str(mirror_path) if mirror_path is not None
            else "no mirror path registered"
        )
        what = detail or f"{len(packs)} selected pack(s) cannot be staged"
        # Bypass MissingPacksError.__init__: this error formats its own message.
        Exception.__init__(
            self,
            f"Mirror for repo '{repo_id}' is unavailable ({where}): {what}. "
            "Is the NAS mounted? Nothing was written to the catalog; "
            "fix the mirror and re-run 'lcsas stage'.",
        )
        self.missing = packs
        self.repo_id = repo_id
        self.mirror_path = mirror_path


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
                # Verify existing staged file matches expected size
                # (guards against partial stages from prior failed runs)
                dst_size = dst.stat().st_size if dst.exists() else 0
                if dst_size == pack.size_bytes:
                    # For small files, verify hash to catch corruption from bit-flips
                    _hash_threshold = 500_000_000  # 500 MB
                    if dst_size <= _hash_threshold:
                        dst_hash = sha256_file(dst)
                        if dst_hash == pack.sha256:
                            # File is valid
                            staged += 1
                            _logger.debug(
                                "Pack %s already staged, skipping (%d/%d)", short, i, total
                            )
                            continue
                        else:
                            # Hash mismatch; re-stage
                            _logger.warning(
                                "Pack %s hash mismatch (expected %s, got %s). "
                                "Re-staging from source.",
                                short, pack.sha256, dst_hash,
                            )
                            dst.unlink(missing_ok=True)
                    else:
                        # Large file; skip expensive hash check, just verify size
                        staged += 1
                        _logger.debug(
                            "Pack %s already staged (large file, size verified), "
                            "skipping (%d/%d)",
                            short, i, total,
                        )
                        continue
                else:
                    _logger.warning(
                        "Pack %s partially staged (expected %d bytes, got %d). "
                        "Re-staging from source.",
                        short, pack.size_bytes, dst_size,
                    )
                    dst.unlink(missing_ok=True)
                    # Fall through to re-stage from source

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

    def assert_staged_complete(self, packs: list[Pack]) -> None:
        """Post-stage invariant: the staged data tree holds exactly ``packs``.

        Walks ``data/`` counting files with 64-hex names and summing their
        sizes; raises :class:`MissingPacksError` on any count or byte-total
        mismatch.  Defense in depth against a silent partial stage — callers
        must invoke this before any catalog write.  Cost: one ``os.walk``
        over hardlinks, no content reads.
        """
        expected_count = len(packs)
        expected_bytes = sum(p.size_bytes for p in packs)
        found_count = 0
        found_bytes = 0
        for dirpath, _dirnames, filenames in os.walk(self._data_dir):
            for name in filenames:
                if _HEX64_RE.match(name):
                    found_count += 1
                    found_bytes += os.stat(os.path.join(dirpath, name)).st_size
        if found_count == expected_count and found_bytes == expected_bytes:
            return

        _logger.error(
            "Staged data tree mismatch in %s: %d file(s) / %d byte(s) found, "
            "expected %d / %d",
            self._data_dir, found_count, found_bytes,
            expected_count, expected_bytes,
        )
        bad: list[str] = []
        for p in packs:
            dst = pack_dest_path(self._data_dir, p.sha256)
            if not dst.is_file() or dst.stat().st_size != p.size_bytes:
                bad.append(p.sha256[:12])
        raise MissingPacksError(
            bad or [f"unexpected extra pack file(s) under {self._data_dir}"]
        )

    def _find_pack_file(self, data_dir: Path, sha256: str) -> Path | None:
        """Locate a pack file in the mirror data directory.

        Checks two-level hash-prefix layout first, then flat.
        """
        return find_pack_file(data_dir, sha256)

    def cleanup(self) -> None:
        """Remove the entire staging directory tree."""
        safe_remove_tree(self._root)
