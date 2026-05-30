"""Protocol and implementation for DVDisaster ECC operations."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from lcsas.utils.subprocess import SubprocessRunnerBase

_logger = logging.getLogger(__name__)


class DVDisasterRunner(Protocol):
    """Abstract interface for error correction code operations."""

    def augment_iso(
        self,
        iso_path: Path,
        redundancy_pct: int = 15,
    ) -> None: ...

    def verify_iso(
        self,
        iso_path: Path,
    ) -> bool: ...

    def repair_iso(
        self,
        iso_path: Path,
    ) -> bool: ...


class SubprocessDVDisasterRunner(SubprocessRunnerBase):
    """Real DVDisaster implementation using subprocess."""

    def __init__(
        self,
        dvdisaster_binary: str = "dvdisaster",
        tmpdir: Path | None = None,
    ) -> None:
        super().__init__(dvdisaster_binary, tmpdir)

    def augment_iso(
        self,
        iso_path: Path,
        redundancy_pct: int = 15,
        timeout: int = 7200,
    ) -> None:
        """Augment an ISO image with RS03 error correction data.

        Operates on a temporary copy to avoid corrupting the ISO if the
        process is interrupted.  On success the augmented copy replaces
        the original atomically via ``os.rename``.
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")

        # Pre-flight: verify there is enough free space for the temp copy.
        iso_size = iso_path.stat().st_size
        disk_free = shutil.disk_usage(iso_path.parent).free
        # Need one full copy of the ISO plus a 1 MiB safety margin.
        if disk_free < iso_size + 1_048_576:
            raise OSError(
                f"Insufficient disk space to create ECC temp copy of '{iso_path.name}': "
                f"{disk_free:,} bytes free, {iso_size + 1_048_576:,} bytes needed."
            )

        tmp = iso_path.with_suffix(".iso.ecc.tmp")
        try:
            shutil.copy2(str(iso_path), str(tmp))
            cmd = [
                self._binary,
                "-i", str(tmp),
                "-mRS03",
                "-n", str(redundancy_pct),
                "-c",
            ]
            try:
                subprocess.run(
                    cmd, capture_output=True, text=True, check=True,
                    env=self._env(), timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                self._handle_timeout("dvdisaster", "ECC augmentation", exc)
            except subprocess.CalledProcessError as exc:
                self._log_stderr("dvdisaster", exc)
                raise
            # Atomic replace on success
            import os
            os.rename(tmp, iso_path)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise

    def verify_iso(
        self,
        iso_path: Path,
        timeout: int = 3600,
    ) -> bool:
        """Verify the ECC integrity of an ISO image."""
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-t",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("dvdisaster", "ECC verification", exc)
        return result.returncode == 0

    def repair_iso(
        self,
        iso_path: Path,
        timeout: int = 3600,
    ) -> bool:
        """Attempt to repair a damaged ISO using its embedded ECC data.

        Returns True iff the image is intact *after* the repair attempt.

        dvdisaster's ``-f`` exits NONZERO (observed: 1) even when it
        SUCCESSFULLY corrects errors — its exit code conflates "corrected
        some errors" with "failed to correct", so it is not a reliable
        success signal (issue #305).  Rather than reverse-engineer an
        undocumented, version-specific exit-code matrix, we measure the
        outcome directly: ``-f`` fixes the image in place, then we re-run
        verification and return whether the image now passes.  This is
        version-independent and answers the only question a caller cares
        about — is the disc good now?
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-f",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("dvdisaster", "ECC repair", exc)
        if result.returncode != 0:
            _logger.info(
                "dvdisaster -f exited %d for %s; confirming outcome via verify",
                result.returncode, iso_path.name,
            )
        # Ground truth: the repair succeeded iff the image now verifies clean.
        # (-f fixes in place, so this re-reads the same file -f just wrote.)
        return self.verify_iso(iso_path, timeout=timeout)
