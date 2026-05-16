"""Restore executor — fetches packs from volumes and runs rustic restore."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from lcsas.exceptions import RestoreError
from lcsas.log import get_logger
from lcsas.rustic.wrapper import RusticRunner
from lcsas.utils.fs import copy_file, ensure_dir
from lcsas.utils.hashing import sha256_file
from lcsas.utils.pack_layout import METADATA_SUBDIRS, find_pack_file, pack_dest_path

if TYPE_CHECKING:
    from lcsas.config.media import MediaType

logger = get_logger()


class ECCRunner(Protocol):
    """Optional ECC verification interface."""

    def verify_iso(self, iso_path: Path) -> bool: ...
    def repair_iso(self, iso_path: Path) -> bool: ...


class PackCorruptionError(Exception):
    """Raised when a pack file fails SHA-256 verification after copy."""


@dataclass
class IngestionResult:
    """Result of a single-volume pack ingestion."""

    ingested: int
    failed: list[str] = field(default_factory=list)


class RestoreExecutor:
    """Executes a restore operation by assembling packs into a cache."""

    def __init__(
        self,
        rustic_runner: RusticRunner,
        ecc_runner: ECCRunner | None = None,
    ) -> None:
        self._rustic = rustic_runner
        self._ecc = ecc_runner

    def verify_iso(
        self,
        iso_path: Path,
        expected_sha256: str | None = None,
    ) -> bool:
        """Verify an ISO's integrity.

        Order of preference:
        1. ECC runner if configured (DVDisaster RS03; can detect AND
           repair bit-rot).
        2. SHA-256 compare against ``expected_sha256`` if supplied
           (Phase 21.2.b — portable, detect-only fallback used on
           macOS / Windows recovery hosts where dvdisaster isn't
           bundled).
        3. Skip with a "not verified" log when neither is available.

        Returns True if verification passed (or could not be performed
        because no integrity source was available).  Returns False only
        when an integrity source was attempted and reported corruption.
        """
        if self._ecc is not None:
            logger.info(f"Verifying ECC on {iso_path.name}")
            if self._ecc.verify_iso(iso_path):
                logger.info(f"ECC verification passed: {iso_path.name}")
                return True

            logger.warning(
                f"ECC verification failed for {iso_path.name}, attempting repair"
            )
            if self._ecc.repair_iso(iso_path):
                logger.info(f"ECC repair succeeded: {iso_path.name}")
                return True

            logger.error(f"ECC repair failed for {iso_path.name}")
            return False

        if expected_sha256:
            return verify_iso_sha256(iso_path, expected_sha256)

        logger.info(
            "No ECC runner configured and no expected SHA-256 supplied — "
            "disc integrity not verified for %s",
            iso_path.name,
        )
        return True

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

        missing_dirs: list[str] = []
        for subdir in METADATA_SUBDIRS:
            src = metadata_source / subdir
            dst = cache_dir / subdir
            if src.is_dir() and not dst.exists():
                shutil.copytree(str(src), str(dst))
            elif not src.is_dir() and not dst.exists():
                missing_dirs.append(subdir)

        if missing_dirs:
            raise FileNotFoundError(
                f"Required metadata missing from source '{metadata_source}': "
                f"{', '.join(missing_dirs)}. "
                "Each directory (index/, snapshots/, keys/) must be present. "
                "Try another disc — every disc in this archive contains a full "
                "copy of the repository metadata."
            )

        config_src = metadata_source / "config"
        config_dst = cache_dir / "config"
        if config_src.is_file() and not config_dst.exists():
            copy_file(config_src, config_dst)
        elif not config_src.is_file() and not config_dst.exists():
            raise FileNotFoundError(
                f"Repository config file missing from metadata source "
                f"(expected at {config_src}). "
                "Try another disc — every disc contains a copy of the config."
            )

    def ingest_volume(
        self,
        cache_dir: Path,
        volume_mount: Path,
        required_packs: list[str],
        *,
        verify: bool = True,
        collect_failures: bool = False,
        iso_path: Path | None = None,
        media_type: MediaType | None = None,
        expected_sha256: str | None = None,
    ) -> IngestionResult:
        """Copy needed packs from a mounted volume into the restore cache.

        Args:
            cache_dir: The local restore cache directory.
            volume_mount: Path where the disc is mounted.
            required_packs: SHA-256 hashes of packs to copy from this volume.
            verify: If True (default), verify SHA-256 of copied packs.
            collect_failures: If True, collect corrupt/missing pack hashes in
                IngestionResult.failed instead of raising PackCorruptionError.
            iso_path: Optional path to the underlying ISO file backing this
                mount.  When supplied along with a configured ECC runner, the
                ISO's DVDisaster RS03 ECC is verified (and repaired if needed)
                before any pack is read — recovers transparently from bit-rot
                that lies within the recovery margin.
            media_type: Optional media type of the volume.  Currently used
                only for diagnostics; ECC verify is always attempted when an
                ECC runner and ``iso_path`` are supplied.
            expected_sha256: Optional ISO SHA-256 hash recorded at burn time
                (Phase 21.3 — sourced from ``session_volumes.iso_sha256`` or
                ``volume_copies.iso_sha256``).  When supplied without an ECC
                runner, the verifier falls back to a portable SHA-256 compare
                (detect-only; cannot repair) — covers macOS / Windows recovery
                hosts where DVDisaster isn't bundled.

        Returns:
            IngestionResult with ingested count and (optionally) failed hashes.

        Raises:
            PackCorruptionError: When a copied pack fails hash verification
                and collect_failures is False.
            RestoreError: When ISO verification fails — either ECC verify+repair
                both failed (Linux path) or the SHA-256 mismatched (portable
                fallback path).  Points the operator at an alternate copy
                from a different location either way.
        """
        # ── ISO integrity check before reading any pack ─────────────
        # Issue #20 + Phase 21.3: verify the mounted ISO's integrity
        # using the strongest mechanism available (ECC > SHA-256
        # fallback) so bit-rot is caught before it propagates to a
        # restored file.  Guards (no-op cases):
        #   * ``iso_path is None`` — caller has no ISO handle (e.g.
        #     reading from a pre-extracted directory in tests).
        #   * No ECC runner AND no expected_sha256 — there's nothing
        #     to compare against; ``verify_iso`` returns True with a
        #     "not verified" log.
        have_verifier = self._ecc is not None or expected_sha256 is not None
        if (
            iso_path is not None
            and have_verifier
            and not self.verify_iso(iso_path, expected_sha256=expected_sha256)
        ):
            if self._ecc is not None:
                msg = (
                    f"ECC verification and repair both failed for "
                    f"'{iso_path.name}'. The disc is damaged beyond "
                    f"DVDisaster RS03's recovery margin."
                )
            else:
                msg = (
                    f"ISO SHA-256 mismatch for '{iso_path.name}'. "
                    f"The disc bytes don't match what was burned; "
                    f"DVDisaster wasn't available on this host so no "
                    f"repair was attempted."
                )
            raise RestoreError(
                msg,
                recovery_hint=(
                    "Try an alternate copy of this volume from a "
                    "different location (off-site / cold-vault). "
                    "Every volume in this archive is typically burned "
                    "to multiple discs across locations — run "
                    "`lcsas catalog locations <volume_label>` to list "
                    "them."
                ),
            )

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
                # Re-verify existing file if verification is enabled
                # (guards against zero-byte or corrupt files from prior aborted runs)
                if verify:
                    actual = sha256_file(dst)
                    if actual != sha256:
                        logger.warning(
                            "Cached pack %s is CORRUPT (SHA-256 mismatch: got %s). "
                            "Removing and will re-ingest from volume.",
                            sha256, actual,
                        )
                        dst.unlink()
                        # Fall through to re-copy from source
                    else:
                        # Cached file is valid; skip (don't re-ingest)
                        continue
                else:
                    # No verification; at least check for zero-byte files
                    if dst.stat().st_size == 0:
                        logger.warning(
                            "Cached pack %s is zero-byte (partial copy). "
                            "Removing and will re-ingest from volume.",
                            sha256,
                        )
                        dst.unlink(missing_ok=True)
                        # Fall through to re-copy from source
                    else:
                        # File exists and is non-zero; assume valid
                        continue

            # Locate pack on the source volume (flat or two-level)
            src = find_pack_file(data_dir, sha256)

            if src is not None:
                copy_file(src, dst)

                if verify:
                    actual = sha256_file(dst)
                    if actual != sha256:
                        dst.unlink(missing_ok=True)
                        if collect_failures:
                            logger.error(
                                "Pack %s is CORRUPT on this volume "
                                "(SHA-256 mismatch: got %s). "
                                "Will try alternate volumes if available.",
                                sha256, actual,
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
            else:
                # Pack not found on this volume — record failure for retry
                if collect_failures:
                    logger.warning(
                        "Pack %s not found on this volume (%s). "
                        "Will try alternate volumes if available.",
                        sha256, data_dir,
                    )
                    failed.append(sha256)
                else:
                    raise PackCorruptionError(
                        f"Pack {sha256} not found on volume {data_dir}"
                    )

        return IngestionResult(ingested=ingested, failed=failed)

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
        *,
        verify_hashes: bool = False,
    ) -> list[str]:
        """Check that every required pack is present in the cache.

        Walks ``cache_dir/data/`` looking for each SHA-256 hash in the
        two-level layout (``data/<prefix>/<sha256>``).

        Args:
            cache_dir: The assembled restore cache directory.
            required_packs: SHA-256 hashes of every pack the restore needs.
            verify_hashes: If True, re-verify the SHA-256 of each cached
                pack — catches corruption after ingest.

        Returns:
            List of missing or corrupted SHA-256 hashes (empty if complete).
        """
        data_dir = cache_dir / "data"
        missing: list[str] = []
        for sha256 in required_packs:
            path = data_dir / sha256[:2] / sha256
            if not path.is_file():
                missing.append(sha256)
            elif verify_hashes:
                actual = sha256_file(path)
                if actual != sha256:
                    logger.error(
                        "Pack %s is CORRUPT in cache (SHA-256 mismatch: got %s)",
                        sha256, actual,
                    )
                    missing.append(sha256)
        return missing


def verify_iso_sha256(iso_path: Path, expected_sha256: str) -> bool:
    """Portable ISO verifier — compares SHA-256 against an expected hash.

    Phase 21.2.b helper: detects corruption without needing xorriso or
    dvdisaster on the recovery host.  Cannot repair (that's still
    DVDisaster's job during the Linux burn flow); this is detect-only,
    which is exactly the right shape for macOS / Windows recovery
    hosts where neither ECC tool is bundled.

    The ``expected_sha256`` value normally comes from the
    ``session_volumes.iso_sha256`` column written at burn time
    (``src/lcsas/db/sessions.py``) or from a per-volume receipt
    JSON.  Case-insensitive compare so hex strings from different
    sources match cleanly.
    """
    if not iso_path.is_file():
        logger.error(f"verify_iso_sha256: file not found: {iso_path}")
        return False
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        logger.error(
            "verify_iso_sha256: malformed expected SHA-256 %r for %s",
            expected_sha256, iso_path.name,
        )
        return False
    actual = sha256_file(iso_path).lower()
    if actual == expected:
        logger.info(
            "SHA-256 verification passed: %s (no ECC runner; detect-only)",
            iso_path.name,
        )
        return True
    logger.error(
        "SHA-256 verification FAILED for %s: expected %s, got %s",
        iso_path.name, expected, actual,
    )
    return False
