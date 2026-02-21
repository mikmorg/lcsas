"""Restore executor — fetches packs from volumes and runs rustic restore."""

from __future__ import annotations

import shutil
from pathlib import Path

from lcsas.log import get_logger
from lcsas.rustic.wrapper import RusticRunner
from lcsas.utils.fs import copy_file, ensure_dir
from lcsas.utils.hashing import sha256_file

logger = get_logger()


class PackCorruptionError(Exception):
    """Raised when a pack file fails SHA-256 verification after copy."""


class RestoreExecutor:
    """Executes a restore operation by assembling packs into a cache."""

    def __init__(self, rustic_runner: RusticRunner) -> None:
        self._rustic = rustic_runner

    def prepare_cache(
        self,
        cache_dir: Path,
        metadata_source: Path,
    ) -> None:
        """Set up a restore cache directory with metadata from a source.

        Copies index/, snapshots/, keys/, and config from the metadata
        source (a disc mount point or local mirror) into the cache.

        Args:
            cache_dir: The local restore cache directory.
            metadata_source: Path containing repository metadata
                (e.g., from a mounted disc's metadata/<repo_id>/ dir).
        """
        ensure_dir(cache_dir)
        ensure_dir(cache_dir / "data")

        for subdir in ["index", "snapshots", "keys"]:
            src = metadata_source / subdir
            dst = cache_dir / subdir
            if src.is_dir() and not dst.exists():
                shutil.copytree(str(src), str(dst))

        config_src = metadata_source / "config"
        config_dst = cache_dir / "config"
        if config_src.is_file() and not config_dst.exists():
            copy_file(config_src, config_dst)

    def ingest_volume(
        self,
        cache_dir: Path,
        volume_mount: Path,
        required_packs: list[str],
        *,
        verify: bool = True,
    ) -> int:
        """Copy needed packs from a mounted volume into the restore cache.

        Args:
            cache_dir: The local restore cache directory.
            volume_mount: Path where the disc is mounted.
            required_packs: SHA-256 hashes of packs to copy from this volume.
            verify: If True (default), verify SHA-256 of copied packs.

        Returns:
            Number of packs successfully ingested.

        Raises:
            PackCorruptionError: When a copied pack fails hash verification.
        """
        data_dir = volume_mount / "data"
        cache_data = cache_dir / "data"
        ensure_dir(cache_data)
        ingested = 0

        for sha256 in required_packs:
            # Place packs in two-level layout (data/<prefix>/<hash>)
            # which restic 0.14+ expects for local repositories.
            if len(sha256) >= 2:
                prefix_dir = cache_data / sha256[:2]
                ensure_dir(prefix_dir)
                dst = prefix_dir / sha256
            else:
                dst = cache_data / sha256

            if dst.exists():
                continue

            # Try flat layout on the source volume
            src = data_dir / sha256
            if not src.is_file() and len(sha256) >= 2:
                # Two-level layout on source
                src = data_dir / sha256[:2] / sha256

            if src.is_file():
                copy_file(src, dst)

                if verify:
                    actual = sha256_file(dst)
                    if actual != sha256:
                        dst.unlink()
                        raise PackCorruptionError(
                            f"Pack {sha256} failed integrity check: "
                            f"expected {sha256}, got {actual}"
                        )
                    logger.debug(
                        f"Verified pack {sha256} ({dst.stat().st_size} bytes)"
                    )

                ingested += 1

        return ingested

    def execute_restore(
        self,
        cache_dir: Path,
        snapshot_id: str,
        target_path: Path,
        password_file: Path,
    ) -> None:
        """Run rustic restore against the assembled cache.

        The cache_dir must contain all required data packs plus metadata.
        """
        self._rustic.restore(
            snapshot_id=snapshot_id,
            repo_path=cache_dir,
            password_file=password_file,
            target_path=target_path,
        )
