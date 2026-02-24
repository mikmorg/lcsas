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
    ) -> None:
        """Augment an ISO image with RS03 error correction data.

        Operates on a temporary copy to avoid corrupting the ISO if the
        process is interrupted.  On success the augmented copy replaces
        the original atomically via ``os.rename``.
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")

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
                subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._env())
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
    ) -> bool:
        """Verify the ECC integrity of an ISO image."""
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-t",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=self._env()
        )
        return result.returncode == 0

    def repair_iso(
        self,
        iso_path: Path,
    ) -> bool:
        """Attempt to repair a damaged ISO using its embedded ECC data."""
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-f",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=self._env()
        )
        return result.returncode == 0
