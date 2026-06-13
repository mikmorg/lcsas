"""Protocol and implementation for Xorriso ISO creation and burning."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from lcsas.utils.subprocess import SubprocessRunnerBase

_logger = logging.getLogger(__name__)


class MediaStatus(StrEnum):
    """State of the optical medium loaded in a drive (FUP-01).

    Distinguishes "safe to burn onto" (``blank``/``appendable``) from
    "already carries data" (``closed``) so a multi-disc session refuses
    to overwrite the disc just burned instead of failing mid-burn.
    ``unknown`` covers drives whose -toc output we cannot classify — the
    caller warns and proceeds rather than bricking an odd drive.
    """

    BLANK = "blank"
    APPENDABLE = "appendable"
    CLOSED = "closed"
    NO_MEDIA = "no_media"
    UNKNOWN = "unknown"

# Max single-extent ISO 9660 file section: 4 GiB - 2 KiB.  A file larger than
# this is stored as multiple extents under ISO 9660 Level 3, which Windows'
# native CDFS driver (the Mount-DiskImage path behind restore.bat) does not
# reassemble — the heir silently sees a truncated file.  We refuse to master
# any tree containing such a file.
_ISO_MAX_FILE_BYTES = 0xFFFF_F800


class OversizeFileError(Exception):
    """A file in the staging tree is too large to store single-extent in ISO 9660.

    ISO 9660 Level 3 splits a >4 GiB file across multiple extents, which
    Windows' built-in CDFS mount silently truncates.  Rather than burn a disc
    that the statistically most-likely heir platform cannot read, mastering is
    refused.
    """


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
        timeout: int = 7200,
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

    def read_disc_volume_id(
        self,
        device: str = "/dev/sr0",
    ) -> str: ...

    def media_status(
        self,
        device: str = "/dev/sr0",
    ) -> MediaStatus: ...


class SubprocessXorrisoRunner(SubprocessRunnerBase):
    """Real Xorriso implementation using subprocess."""

    def __init__(
        self,
        xorriso_binary: str = "xorriso",
        tmpdir: Path | None = None,
    ) -> None:
        super().__init__(xorriso_binary, tmpdir)

    def _assert_no_multiextent_files(self, source_dir: Path) -> None:
        """Refuse to master a tree containing a >4 GiB file.

        Such a file becomes multi-extent under ISO 9660 Level 3 and is
        silently truncated by Windows' native CDFS mount.  Called before any
        xorriso process is spawned so the failure is loud and local.
        """
        offenders = [
            (p, size)
            for p in source_dir.rglob("*")
            if p.is_file() and (size := p.stat().st_size) > _ISO_MAX_FILE_BYTES
        ]
        if offenders:
            detail = "; ".join(
                f"'{p.relative_to(source_dir)}' is {size:,} bytes"
                for p, size in offenders
            )
            raise OversizeFileError(
                f"{detail} (> 4 GiB - 2 KiB). ISO 9660 would store it "
                f"multi-extent, which Windows' native mount silently truncates. "
                f"Refusing to master. Split the file or reduce rustic pack size."
            )

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
        self._assert_no_multiextent_files(source_dir)
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
        timeout: int = 7200,
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
        self._assert_no_multiextent_files(source_dir)

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
                env=self._env(), timeout=timeout,
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
        if not iso_path.is_file():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
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

    def read_disc_volume_id(
        self,
        device: str = "/dev/sr0",
        timeout: int = 300,
    ) -> str:
        """Read the ISO 9660 PVD Volume ID from the disc in *device*.

        The Volume ID is written at mastering time (``-V <label>`` in
        :meth:`create_iso`), so it is the disc's machine-readable
        identity — verification compares it against the catalog label
        to catch the wrong-disc-in-drive case (FMA-03).

        Returns ``''`` on any failure (no disc, unreadable PVD, missing
        binary, timeout): an unknown identity must never match a label.
        """
        cmd = [
            self._binary,
            "-indev", device,
            "-pvd_info",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            # xorriso -pvd_info format: "Volume id    : 'LABEL'"
            stripped = line.strip()
            if stripped.lower().startswith("volume id") and ":" in stripped:
                value = stripped.split(":", 1)[1].strip()
                if value.startswith("'") and value.endswith("'") and len(value) >= 2:
                    return value[1:-1]
                return value
        return ""

    def media_status(
        self,
        device: str = "/dev/sr0",
        timeout: int = 300,
    ) -> MediaStatus:
        """Classify the medium in *device* before a burn (FUP-01).

        Parses ``xorriso -outdev <device> -toc``.  A ``closed`` disc
        already carries finalized data and must not be burned onto; a
        ``blank`` or ``appendable`` disc is safe.  Anything we cannot
        classify returns :attr:`MediaStatus.UNKNOWN` — the caller warns
        and proceeds rather than bricking an odd drive.

        Returns :attr:`MediaStatus.UNKNOWN` on any failure to spawn the
        binary or on timeout: an unrecognised drive must never block a
        burn outright.
        """
        cmd = [
            self._binary,
            "-outdev", device,
            "-toc",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return MediaStatus.UNKNOWN
        return self._classify_toc(
            (result.stdout or "") + "\n" + (result.stderr or "")
        )

    @staticmethod
    def _classify_toc(output: str) -> MediaStatus:
        """Map xorriso -toc output to a :class:`MediaStatus`.

        xorriso reports the medium state on a ``Media status :`` line
        (``is blank`` / ``is written`` / ``is closed`` / ``is
        appendable``) and the absence of media as ``No media present`` or
        ``Media current: is not present``.
        """
        low = output.lower()
        if (
            "no media" in low
            or "is not present" in low
            or "no medium" in low
            or "medium not present" in low
        ):
            return MediaStatus.NO_MEDIA
        if "is blank" in low:
            return MediaStatus.BLANK
        if "is appendable" in low:
            return MediaStatus.APPENDABLE
        if "is closed" in low or "is written" in low:
            return MediaStatus.CLOSED
        return MediaStatus.UNKNOWN
