"""Protocol and implementation for Xorriso ISO creation and burning."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Protocol

from lcsas.utils.subprocess import SubprocessRunnerBase

_logger = logging.getLogger(__name__)


class XorrisoRunner(Protocol):
    """Abstract interface for ISO mastering and burning."""

    def create_iso(
        self,
        source_dir: Path,
        output_iso: Path,
        volume_label: str,
    ) -> Path: ...

    def burn_iso(
        self,
        iso_path: Path,
        device: str = "/dev/sr0",
    ) -> None: ...

    def verify_disc(
        self,
        device: str = "/dev/sr0",
    ) -> bool: ...


class SubprocessXorrisoRunner(SubprocessRunnerBase):
    """Real Xorriso implementation using subprocess."""

    def __init__(
        self,
        xorriso_binary: str = "xorriso",
        tmpdir: Path | None = None,
    ) -> None:
        super().__init__(xorriso_binary, tmpdir)

    def create_iso(
        self,
        source_dir: Path,
        output_iso: Path,
        volume_label: str,
    ) -> Path:
        """Create an ISO 9660 image with Rock Ridge and Joliet extensions.

        Writes to a temporary ``.iso.tmp`` file first, then renames to
        the final path on success.  If the subprocess fails the partial
        temp file is removed.
        """
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source directory not found: {source_dir}")
        tmp_iso = output_iso.with_suffix(".iso.tmp")
        cmd = [
            self._binary,
            "-as", "mkisofs",
            "-r",                    # Rock Ridge (POSIX permissions)
            "-J",                    # Joliet (Windows compat)
            "-joliet-long",          # Long Joliet names
            "-iso-level", "3",       # Support files > 4 GB
            "-V", volume_label,      # Volume label
            "-o", str(tmp_iso),      # Temp output ISO file
            str(source_dir),         # Source directory
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._env())
            os.rename(tmp_iso, output_iso)
        except subprocess.CalledProcessError as exc:
            self._log_stderr("xorriso", exc)
            if tmp_iso.exists():
                tmp_iso.unlink()
            raise
        except Exception:
            if tmp_iso.exists():
                tmp_iso.unlink()
            raise
        return output_iso

    def burn_iso(
        self,
        iso_path: Path,
        device: str = "/dev/sr0",
    ) -> None:
        """Burn an ISO image to optical media using DAO mode."""
        cmd = [
            self._binary,
            "-as", "cdrecord",
            "-v",
            f"dev={device}",
            "-dao",
            "fs=64m",
            str(iso_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._env())
        except subprocess.CalledProcessError as exc:
            self._log_stderr("xorriso", exc)
            raise

    def verify_disc(
        self,
        device: str = "/dev/sr0",
    ) -> bool:
        """Verify a burned disc by reading back the ISO structure."""
        cmd = [
            self._binary,
            "-indev", device,
            "-check_media",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=self._env()
        )
        return result.returncode == 0
