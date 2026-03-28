"""Restore executor — fetches packs from volumes and runs rustic restore."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from lcsas.log import get_logger
from lcsas.rustic.wrapper import RusticRunner
from lcsas.utils.fs import copy_file, ensure_dir
from lcsas.utils.hashing import sha256_file
from lcsas.utils.pack_layout import METADATA_SUBDIRS, find_pack_file, pack_dest_path

logger = get_logger()


class ECCRunner(Protocol):
    """Optional ECC verification interface."""

    def verify_iso(self, iso_path: Path) -> bool: ...
    def repair_iso(self, iso_path: Path) -> bool: ...


class PackCorruptionError(Exception):
    """Raised when a pack file fails SHA-256 verification after copy."""


class RestoreExecutor:
    """Executes a restore operation by assembling packs into a cache."""

    def __init__(
        self,
        rustic_runner: RusticRunner,
        ecc_runner: ECCRunner | None = None,
    ) -> None:
        self._rustic = rustic_runner
        self._ecc = ecc_runner

    def verify_iso(self, iso_path: Path) -> bool:
        """Verify an ISO's ECC data using the configured ECC runner.

        Returns True if ECC is valid or no ECC runner is configured.
        Attempts automatic repair if verification fails.
        """
        if self._ecc is None:
            logger.debug("No ECC runner configured — skipping ISO verification")
            return True

        logger.info(f"Verifying ECC on {iso_path.name}")
        if self._ecc.verify_iso(iso_path):
            logger.info(f"ECC verification passed: {iso_path.name}")
            return True

        logger.warning(f"ECC verification failed for {iso_path.name}, attempting repair")
        if self._ecc.repair_iso(iso_path):
            logger.info(f"ECC repair succeeded: {iso_path.name}")
            return True

        logger.error(f"ECC repair failed for {iso_path.name}")
        return False

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

        for subdir in METADATA_SUBDIRS:
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
        collect_failures: bool = False,
    ) -> int | tuple[int, list[str]]:
        """Copy needed packs from a mounted volume into the restore cache.

        Args:
            cache_dir: The local restore cache directory.
            volume_mount: Path where the disc is mounted.
            required_packs: SHA-256 hashes of packs to copy from this volume.
            verify: If True (default), verify SHA-256 of copied packs.
            collect_failures: If True, return failed pack hashes instead of
                raising PackCorruptionError.  Returns (ingested, failed_list).

        Returns:
            Number of packs successfully ingested (if collect_failures=False),
            or (ingested_count, failed_sha256_list) if collect_failures=True.

        Raises:
            PackCorruptionError: When a copied pack fails hash verification
                and collect_failures is False.
        """
        data_dir = volume_mount / "data"
        cache_data = cache_dir / "data"
        ensure_dir(cache_data)
        ingested = 0
        failed: list[str] = []

        for i, sha256 in enumerate(required_packs, 1):
            if i % 50 == 0 or i == len(required_packs):
                logger.info(
                    "Ingesting packs: %d/%d", i, len(required_packs),
                )
            # Place packs in two-level layout via shared helper
            dst = pack_dest_path(cache_data, sha256)
            ensure_dir(dst.parent)

            if dst.exists():
                continue

            # Locate pack on the source volume (flat or two-level)
            src = find_pack_file(data_dir, sha256)

            if src is not None:
                copy_file(src, dst)

                if verify:
                    actual = sha256_file(dst)
                    if actual != sha256:
                        dst.unlink()
                        if collect_failures:
                            logger.warning(
                                f"Pack {sha256} corrupt on this volume "
                                f"(expected {sha256}, got {actual})"
                            )
                            failed.append(sha256)
                            continue
                        raise PackCorruptionError(
                            f"Pack {sha256} failed integrity check: "
                            f"expected {sha256}, got {actual}"
                        )
                    logger.debug(
                        f"Verified pack {sha256} ({dst.stat().st_size} bytes)"
                    )

                ingested += 1

        if collect_failures:
            return ingested, failed
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

    @staticmethod
    def verify_cache_completeness(
        cache_dir: Path,
        required_packs: list[str],
    ) -> list[str]:
        """Check that every required pack is present in the cache.

        Walks ``cache_dir/data/`` looking for each SHA-256 hash in the
        two-level layout (``data/<prefix>/<sha256>``).

        Args:
            cache_dir: The assembled restore cache directory.
            required_packs: SHA-256 hashes of every pack the restore needs.

        Returns:
            List of missing SHA-256 hashes (empty if complete).
        """
        data_dir = cache_dir / "data"
        missing: list[str] = []
        for sha256 in required_packs:
            path = data_dir / sha256[:2] / sha256 if len(sha256) >= 2 else data_dir / sha256
            if not path.is_file():
                missing.append(sha256)
        return missing
