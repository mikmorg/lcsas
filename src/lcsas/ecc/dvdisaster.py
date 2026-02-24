"""Protocol and implementation for DVDisaster ECC operations."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Protocol

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


class SubprocessDVDisasterRunner:
    """Real DVDisaster implementation using subprocess."""

    def __init__(
        self,
        dvdisaster_binary: str = "dvdisaster",
        tmpdir: Path | None = None,
    ) -> None:
        self._binary = dvdisaster_binary
        self._tmpdir = tmpdir

    def _env(self) -> dict[str, str] | None:
        if self._tmpdir is None:
            return None
        return {**os.environ, "TMPDIR": str(self._tmpdir)}

    def augment_iso(
        self,
        iso_path: Path,
        redundancy_pct: int = 15,
    ) -> None:
        """Augment an ISO image with RS03 error correction data.

        The ECC data is appended directly to the ISO file, consuming
        additional space proportional to redundancy_pct.
        """
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-mRS03",
            "-n", str(redundancy_pct),
            "-c",
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._env())
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                for line in exc.stderr.strip().splitlines():
                    _logger.error("  dvdisaster: %s", line)
            raise

    def verify_iso(
        self,
        iso_path: Path,
    ) -> bool:
        """Verify the ECC integrity of an ISO image."""
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
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-f",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=self._env()
        )
        return result.returncode == 0
