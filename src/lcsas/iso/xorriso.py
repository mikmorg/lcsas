"""Protocol and implementation for Xorriso ISO creation and burning."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


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


class SubprocessXorrisoRunner:
    """Real Xorriso implementation using subprocess."""

    def __init__(self, xorriso_binary: str = "xorriso") -> None:
        self._binary = xorriso_binary

    def create_iso(
        self,
        source_dir: Path,
        output_iso: Path,
        volume_label: str,
    ) -> Path:
        """Create an ISO 9660 image with Rock Ridge and Joliet extensions."""
        cmd = [
            self._binary,
            "-as", "mkisofs",
            "-r",                    # Rock Ridge (POSIX permissions)
            "-J",                    # Joliet (Windows compat)
            "-joliet-long",          # Long Joliet names
            "-iso-level", "3",       # Support files > 4 GB
            "-V", volume_label,      # Volume label
            "-o", str(output_iso),   # Output ISO file
            str(source_dir),         # Source directory
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
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
        subprocess.run(cmd, capture_output=True, text=True, check=True)

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
            cmd, capture_output=True, text=True, check=False
        )
        return result.returncode == 0
