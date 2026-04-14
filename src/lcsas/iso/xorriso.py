"""Protocol and implementation for Xorriso ISO creation and burning."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

from lcsas.utils.subprocess import SubprocessRunnerBase

_logger = logging.getLogger(__name__)


def _translate_burn_error(stderr: str, device: str) -> None:
    """Log a human-readable explanation for common xorriso burn failures.

    Called when ``burn_iso`` raises CalledProcessError so the operator gets
    an actionable message before the raw exception propagates.
    """
    low = stderr.lower()
    if "no medium found" in low or "no disc" in low:
        _logger.error(
            "No disc found in drive %s. Insert a blank writable disc and retry.",
            device,
        )
    elif "permission denied" in low or "no read access" in low or "cannot open" in low:
        _logger.error(
            "Permission denied accessing %s. "
            "Add your user to the 'cdrom' group or run with elevated privileges.",
            device,
        )
    elif "device or resource busy" in low or "busy" in low:
        _logger.error(
            "Device %s is busy. Close any other applications using the drive.",
            device,
        )
    elif "input/output error" in low or "i/o error" in low:
        _logger.error(
            "I/O error on %s. The disc may be defective — try a different disc.",
            device,
        )
    elif "medium not present" in low or "not inserted" in low:
        _logger.error(
            "Drive %s reports no disc present. Insert a disc and retry.",
            device,
        )


class XorrisoRunner(Protocol):
    """Abstract interface for ISO mastering and burning."""

    def create_iso(
        self,
        source_dir: Path,
        output_iso: Path,
        volume_label: str,
        timeout: int = 7200,
        expected_bytes: int = 0,
        progress_interval: int = 30,
    ) -> Path: ...

    def create_bootable_iso(
        self,
        source_dir: Path,
        output_iso: Path,
        volume_label: str,
        bios_boot: bool = True,
        uefi_boot: bool = True,
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
        timeout: int = 7200,
        expected_bytes: int = 0,
        progress_interval: int = 30,
    ) -> Path:
        """Create an ISO 9660 image with Rock Ridge and Joliet extensions.

        Writes to a temporary ``.iso.tmp`` file first, then renames to
        the final path on success.  If the subprocess fails the partial
        temp file is removed.

        Logs progress every *progress_interval* seconds by monitoring the
        growing temp-file size against *expected_bytes* (if provided).
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

        stop_event = threading.Event()

        def _log_progress() -> None:
            start = time.monotonic()
            while not stop_event.wait(timeout=progress_interval):
                elapsed = int(time.monotonic() - start)
                written = tmp_iso.stat().st_size if tmp_iso.exists() else 0
                written_mb = written // (1024 * 1024)
                if expected_bytes > 0:
                    pct = min(100, written * 100 // expected_bytes)
                    expected_mb = expected_bytes // (1024 * 1024)
                    _logger.info(
                        "xorriso ISO creation: %d MB / %d MB (%d%%) — %ds elapsed",
                        written_mb, expected_mb, pct, elapsed,
                    )
                else:
                    _logger.info(
                        "xorriso ISO creation: %d MB written — %ds elapsed",
                        written_mb, elapsed,
                    )

        progress_thread = threading.Thread(target=_log_progress, daemon=True)
        progress_thread.start()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(),
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout) from exc
            finally:
                stop_event.set()
                progress_thread.join(timeout=2)

            if proc.returncode != 0:
                for line in stderr.strip().splitlines():
                    _logger.error("  xorriso: %s", line)
                if tmp_iso.exists():
                    tmp_iso.unlink()
                raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)

            os.rename(tmp_iso, output_iso)
        except subprocess.TimeoutExpired as exc:
            if tmp_iso.exists():
                tmp_iso.unlink()
            self._handle_timeout("xorriso", "ISO creation", exc)
        except subprocess.CalledProcessError:
            if tmp_iso.exists():
                tmp_iso.unlink()
            raise
        except Exception:
            stop_event.set()
            if tmp_iso.exists():
                tmp_iso.unlink()
            raise
        return output_iso

    def create_bootable_iso(
        self,
        source_dir: Path,
        output_iso: Path,
        volume_label: str,
        bios_boot: bool = True,
        uefi_boot: bool = True,
    ) -> Path:
        """Create a bootable ISO with El Torito records for BIOS and/or UEFI.

        The *source_dir* must already contain the boot infrastructure:

        * ``isolinux/isolinux.bin`` — for Legacy BIOS boot
        * ``boot/efiboot.img`` — for UEFI boot

        Missing boot files are silently skipped (the corresponding boot
        mode will simply be unavailable).
        """
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source directory not found: {source_dir}")

        tmp_iso = output_iso.with_suffix(".iso.tmp")
        cmd = [
            self._binary,
            "-as", "mkisofs",
            "-r",
            "-J",
            "-joliet-long",
            "-iso-level", "3",
            "-V", volume_label,
        ]

        # Legacy BIOS boot via isolinux (El Torito primary)
        isolinux_bin = source_dir / "isolinux" / "isolinux.bin"
        if bios_boot and isolinux_bin.is_file():
            cmd.extend([
                "-b", "isolinux/isolinux.bin",
                "-c", "isolinux/boot.cat",
                "-no-emul-boot",
                "-boot-load-size", "4",
                "-boot-info-table",
            ])

        # UEFI boot via EFI image (El Torito alternate)
        efiboot_img = source_dir / "boot" / "efiboot.img"
        if uefi_boot and efiboot_img.is_file():
            cmd.extend([
                "-eltorito-alt-boot",
                "-e", "boot/efiboot.img",
                "-no-emul-boot",
            ])

        cmd.extend(["-o", str(tmp_iso), str(source_dir)])

        try:
            subprocess.run(
                cmd, capture_output=True, text=True, check=True,
                env=self._env(), timeout=7200,
            )
            os.rename(tmp_iso, output_iso)
        except subprocess.TimeoutExpired as exc:
            if tmp_iso.exists():
                tmp_iso.unlink()
            self._handle_timeout("xorriso", "bootable ISO creation", exc)
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
        timeout: int = 14400,
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
            subprocess.run(
                cmd, capture_output=True, text=True, check=True,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("xorriso", f"burning to {device}", exc)
        except subprocess.CalledProcessError as exc:
            self._log_stderr("xorriso", exc)
            _translate_burn_error(exc.stderr or "", device)
            raise
        except FileNotFoundError:
            raise RuntimeError(
                f"Required tool '{self._binary}' not found on PATH. "
                f"Install xorriso before burning."
            ) from None

    def verify_disc(
        self,
        device: str = "/dev/sr0",
        timeout: int = 3600,
    ) -> bool:
        """Verify a burned disc by reading back the ISO structure."""
        cmd = [
            self._binary,
            "-indev", device,
            "-check_media",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("xorriso", f"disc verification of {device}", exc)
        return result.returncode == 0
