"""QUARANTINED bootable ISO builder (BOOT-07) — NOT part of the lcsas package.

Takes an already-built meta-volume staging directory (from
``lcsas.meta.builder.MetaVolumeBuilder``) and the quarantined C89
recovery boot artifacts (``experimental/boot/``: vmlinuz +
initramfs.cpio.gz), then creates a hybrid ISO image that boots on
both UEFI and Legacy BIOS systems.

This module was moved out of ``src/lcsas/meta/`` together with the
rest of the bootable-media scaffolding (see README.md in this
directory).  Its Alpine live-boot mode was deleted with the Alpine
stack (`src/lcsas/meta/live/`, plan BOOT-07): the build fetched
unpinned packages from Alpine repos over the network, violating the
every-shipped-runtime-artifact-is-pinned doctrine.  Only the
``recovery_boot_dir`` mode survives.  Revival precondition: the
QEMU/OVMF boot smoke gate of plan BOOT-08.

Boot structure added to the meta-volume::

    boot/
    ├── grub/
    │   └── grub.cfg
    ├── vmlinuz
    └── initramfs.cpio.gz
    EFI/
    └── BOOT/
        └── BOOTX64.EFI
    isolinux/
    ├── isolinux.bin
    ├── isolinux.cfg
    └── ldlinux.c32
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)

# Size of the EFI boot image in MiB — must be large enough
# to hold GRUB EFI binary + grub.cfg + font
_EFI_IMG_SIZE_MIB = 4

# Staged-on-disc artifact names in recovery mode.  These are the
# contract between _install_boot_files and every boot menu entry
# (experimental/boot/efi/grub.cfg + the generated isolinux.cfg);
# pinned by tests/recovery_hardening/test_boot_config_paths.py.
_RECOVERY_KERNEL_NAME = "vmlinuz"
_RECOVERY_INITRD_NAME = "initramfs.cpio.gz"

# Paths loaded by boot menu entries: GRUB ``linux``/``initrd`` lines
# and isolinux ``KERNEL`` / ``APPEND ... initrd=<path>``.
_MENU_PATH_RE = re.compile(
    r"^\s*(?:linux|initrd|KERNEL)\s+(/\S+)|initrd=(/\S+)",
    re.MULTILINE,
)

# newc (SVR4) cpio header: 6-byte magic + 13 × 8-byte ASCII-hex fields.
_NEWC_MAGIC = b"070701"
_NEWC_HEADER_LEN = 110


def _assert_no_empty_regular_files(cpio_gz: Path) -> None:
    """Parse the gzipped newc cpio; raise ValueError on any 0-byte regular file.

    A zero-byte regular file in a recovery initramfs is a placeholder
    left by a broken build (e.g. an empty ``/bin/busybox`` that every
    shell symlink points at — a black screen at boot).  Such archives
    must be rejected, never burned.
    """
    data = gzip.decompress(cpio_gz.read_bytes())
    offenders: list[str] = []
    offset = 0
    while offset + _NEWC_HEADER_LEN <= len(data):
        header = data[offset : offset + _NEWC_HEADER_LEN]
        if header[:6] != _NEWC_MAGIC:
            raise ValueError(
                f"{cpio_gz}: not a newc cpio archive (bad magic at byte {offset})"
            )
        fields = [
            int(header[6 + 8 * i : 6 + 8 * (i + 1)], 16) for i in range(13)
        ]
        mode, filesize, namesize = fields[1], fields[6], fields[11]
        name_start = offset + _NEWC_HEADER_LEN
        # namesize includes the trailing NUL.
        name = data[name_start : name_start + namesize - 1].decode(
            "ascii", errors="replace"
        )
        if name == "TRAILER!!!":
            break
        if stat.S_ISREG(mode) and filesize == 0:
            offenders.append(name)
        # Header+name padded to 4 bytes, then data padded to 4 bytes.
        data_start = (name_start + namesize + 3) & ~3
        offset = (data_start + filesize + 3) & ~3
    if offenders:
        raise ValueError(
            f"{cpio_gz}: initramfs contains zero-byte regular files "
            f"(placeholder build artifacts): {', '.join(sorted(offenders))}"
        )


class BootableISOBuilder:
    """Add boot infrastructure to a meta-volume and create a bootable ISO.

    Usage::

        bib = BootableISOBuilder(
            staging_dir=Path("/tmp/meta"),
            recovery_boot_dir=Path("experimental/boot"),
            output_iso=Path("/tmp/LCSAS_META.iso"),
        )
        bib.build()
    """

    def __init__(
        self,
        staging_dir: Path,
        recovery_boot_dir: Path,
        output_iso: Path | None = None,
        volume_label: str = "LCSAS_META",
        xorriso_binary: str = "xorriso",
        recovery_arch: str = "x86_64",
    ) -> None:
        """
        Args:
            staging_dir: Meta-volume root (already populated by
                ``MetaVolumeBuilder``).
            recovery_boot_dir: Points at an ``experimental/boot/``
                tree (Linux LTS configs + initramfs).  The bootable
                ISO uses the quarantined C89 recovery toolchain boot
                stack.
            output_iso: Path for the final ``.iso`` file.
            volume_label: ISO 9660 volume label.
            xorriso_binary: Path or name of the ``xorriso`` binary.
            recovery_arch: Target architecture for ``recovery_boot_dir``
                (x86_64, aarch64, or riscv64).
        """
        if output_iso is None:
            raise ValueError("output_iso is required")

        self._staging = staging_dir
        self._recovery_boot = recovery_boot_dir
        self._recovery_arch = recovery_arch
        self._output = output_iso
        self._label = volume_label
        self._xorriso = xorriso_binary

    # ── Public API ───────────────────────────────────────────────

    def build(self) -> Path:
        """Assemble boot files and create the bootable ISO.

        Returns the path to the created ISO file.
        """
        self._validate_inputs()
        self._install_boot_files()
        self._install_isolinux()
        self._validate_boot_config_paths()
        self._install_efi()
        self._create_iso()
        return self._output

    # ── Step 1: validate ─────────────────────────────────────────

    def _validate_inputs(self) -> None:
        """Check that all required input files exist."""
        if not self._staging.is_dir():
            raise FileNotFoundError(
                f"Staging directory not found: {self._staging}"
            )
        # experimental/boot/ layout
        arch = self._recovery_arch
        required = [
            self._recovery_boot / "linux" / f"vmlinuz-{arch}",
            self._recovery_boot / f"initramfs-{arch}.cpio.gz",
        ]
        for p in required:
            if not p.is_file():
                raise FileNotFoundError(
                    f"Recovery boot artifact not found: {p}\n"
                    f"Build with: make CC=<cross> bin/{arch}/lcsas-restore "
                    f"&& bash experimental/boot/initramfs/"
                    f"build_initramfs.sh {arch} {p}"
                )
        # A present-but-placeholder archive (zero-byte /init or
        # /bin/busybox) must be rejected here, not at first boot.
        _assert_no_empty_regular_files(
            self._recovery_boot / f"initramfs-{arch}.cpio.gz"
        )

    # ── Step 2: boot files ───────────────────────────────────────

    def _install_boot_files(self) -> None:
        """Copy kernel and initramfs into staging (C89 recovery boot stack)."""
        boot_dir = self._staging / "boot"
        boot_dir.mkdir(parents=True, exist_ok=True)

        arch = self._recovery_arch
        kernel_src = self._recovery_boot / "linux" / f"vmlinuz-{arch}"
        init_src = self._recovery_boot / f"initramfs-{arch}.cpio.gz"
        shutil.copy2(str(kernel_src), str(boot_dir / _RECOVERY_KERNEL_NAME))
        shutil.copy2(str(init_src), str(boot_dir / _RECOVERY_INITRD_NAME))

        grub_dir = boot_dir / "grub"
        grub_dir.mkdir(parents=True, exist_ok=True)
        recovery_grub = self._recovery_boot.parent / "boot" / "efi" / "grub.cfg"
        if recovery_grub.is_file():
            shutil.copy2(str(recovery_grub), str(grub_dir / "grub.cfg"))
        else:
            self._write_default_grub_cfg(
                grub_dir / "grub.cfg",
                kernel=f"/boot/{_RECOVERY_KERNEL_NAME}",
                initrd=f"/boot/{_RECOVERY_INITRD_NAME}",
            )

    # ── Step 3: isolinux (Legacy BIOS) ───────────────────────────

    def _install_isolinux(self) -> None:
        """Set up isolinux bootloader for Legacy BIOS boot.

        Looks for isolinux binaries in standard system locations.
        If not found, BIOS boot is skipped (UEFI-only).
        """
        isolinux_dir = self._staging / "isolinux"
        isolinux_dir.mkdir(parents=True, exist_ok=True)

        # Recovery mode stages initramfs.cpio.gz — generate the config
        # from the staged names (sole source; BOOT-03).
        self._write_default_isolinux_cfg(
            isolinux_dir / "isolinux.cfg",
            kernel=f"/boot/{_RECOVERY_KERNEL_NAME}",
            initrd=f"/boot/{_RECOVERY_INITRD_NAME}",
        )

        # Find isolinux.bin and ldlinux.c32 on the system
        search_dirs = [
            Path("/usr/lib/ISOLINUX"),
            Path("/usr/lib/syslinux/bios"),
            Path("/usr/share/syslinux"),
            Path("/usr/lib/syslinux"),
            Path("/usr/lib/syslinux/modules/bios"),
        ]
        isolinux_bin = self._find_file("isolinux.bin", search_dirs)
        ldlinux_c32 = self._find_file("ldlinux.c32", search_dirs)

        if isolinux_bin:
            shutil.copy2(str(isolinux_bin), str(isolinux_dir / "isolinux.bin"))
            _logger.info("Installed isolinux.bin from %s", isolinux_bin)
        else:
            _logger.warning(
                "isolinux.bin not found — Legacy BIOS boot will not work. "
                "Install the 'isolinux' or 'syslinux' package."
            )

        if ldlinux_c32:
            shutil.copy2(str(ldlinux_c32), str(isolinux_dir / "ldlinux.c32"))

    # ── Step 3.5: menu-entry / staged-tree consistency ───────────

    def _validate_boot_config_paths(self) -> None:
        """Cross-check installed boot menus against the staged tree.

        Every kernel/initrd path referenced by the installed grub.cfg
        and isolinux.cfg must exist in the staging directory — a
        mismatch means a disc whose menu entries all 404 at boot
        (audit finding BOOT-03).
        """
        configs = [
            self._staging / "boot" / "grub" / "grub.cfg",
            self._staging / "isolinux" / "isolinux.cfg",
        ]
        missing: set[str] = set()
        for cfg in configs:
            if not cfg.is_file():
                continue
            for match in _MENU_PATH_RE.finditer(cfg.read_text()):
                ref = match.group(1) or match.group(2)
                if not (self._staging / ref.lstrip("/")).is_file():
                    missing.add(f"{ref} (from {cfg.relative_to(self._staging)})")
        if missing:
            raise ValueError(
                "boot menu references missing path: " + ", ".join(sorted(missing))
            )

    # ── Step 4: EFI boot ─────────────────────────────────────────

    def _install_efi(self) -> None:
        """Set up UEFI boot via a GRUB EFI binary in an ESP image.

        Creates a FAT12 EFI system partition image containing
        BOOTX64.EFI and embeds it in the staging directory.
        Also places the GRUB EFI binary at ``EFI/BOOT/BOOTX64.EFI``
        in the ISO filesystem for systems that can read ISO 9660
        directly.
        """
        # Option 1: Pre-built BOOTX64.EFI (e.g. from grub-efi package)
        bootx64 = self._find_file("BOOTX64.EFI", [
            Path("/usr/lib/grub/x86_64-efi"),
            Path("/usr/share/grub/x86_64-efi"),
            Path("/boot/efi/EFI/BOOT"),
            Path("/boot/EFI/BOOT"),
        ])

        # Option 2: Build GRUB EFI image with grub-mkimage
        if not bootx64:
            bootx64 = self._build_grub_efi()

        if not bootx64:
            _logger.warning(
                "No GRUB EFI binary found — UEFI boot will not work. "
                "Install grub-efi-amd64-bin or equivalent."
            )
            return

        # Place at EFI/BOOT/BOOTX64.EFI in ISO filesystem
        efi_boot_dir = self._staging / "EFI" / "BOOT"
        efi_boot_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(bootx64), str(efi_boot_dir / "BOOTX64.EFI"))

        # Also copy grub.cfg next to BOOTX64.EFI for UEFI GRUB prefix
        grub_cfg = self._staging / "boot" / "grub" / "grub.cfg"
        if grub_cfg.is_file():
            shutil.copy2(str(grub_cfg), str(efi_boot_dir / "grub.cfg"))

        # Build EFI boot image (FAT filesystem image for El Torito)
        self._build_efi_image(bootx64)

    def _build_grub_efi(self) -> Path | None:
        """Attempt to build a GRUB EFI binary using grub-mkimage."""
        grub_mkimage = shutil.which("grub-mkimage")
        if not grub_mkimage:
            return None

        modules = (
            "part_gpt part_msdos fat iso9660 normal boot linux "
            "configfile loopback chain efifwsetup efi_gop efi_uga "
            "ls search search_label search_fs_uuid search_fs_file "
            "gfxterm gfxterm_background gfxterm_menu test all_video "
            "loadenv exfat ext2 ntfs"
        )

        output = self._staging / "boot" / "grub" / "BOOTX64.EFI"
        output.parent.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                [
                    grub_mkimage,
                    "-O", "x86_64-efi",
                    "-o", str(output),
                    "-p", "/boot/grub",
                ] + modules.split(),
                capture_output=True,
                text=True,
                check=True,
            )
            _logger.info("Built GRUB EFI image: %s", output)
            return output
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            _logger.debug("grub-mkimage failed: %s", exc)
            return None

    def _build_efi_image(self, bootx64: Path) -> None:
        """Create a FAT12/16 EFI system partition image for El Torito.

        The image is placed at ``boot/efiboot.img`` in the staging dir.
        """
        efi_img = self._staging / "boot" / "efiboot.img"

        # Calculate size: EFI binary + grub.cfg + overhead
        efi_size = bootx64.stat().st_size
        grub_cfg = self._staging / "boot" / "grub" / "grub.cfg"
        cfg_size = grub_cfg.stat().st_size if grub_cfg.is_file() else 0
        # Add generous overhead for FAT metadata
        total_size = efi_size + cfg_size + (512 * 1024)
        # Round up to MiB
        size_mib = max(_EFI_IMG_SIZE_MIB, (total_size + 1048575) // 1048576)

        # Create blank image
        with open(efi_img, "wb") as f:
            f.truncate(size_mib * 1048576)

        # Format as FAT and copy files using mtools or mkfs.fat + mcopy
        mformat = shutil.which("mformat")
        mcopy = shutil.which("mcopy")

        if mformat and mcopy:
            self._build_efi_image_mtools(efi_img, bootx64)
        else:
            self._build_efi_image_mkfs(efi_img, bootx64)

    def _build_efi_image_mtools(self, efi_img: Path, bootx64: Path) -> None:
        """Build EFI image using mtools (no root needed)."""
        # mformat the image
        subprocess.run(
            ["mformat", "-i", str(efi_img), "-F", "::"],
            capture_output=True, text=True, check=True,
        )
        # Create EFI/BOOT directory
        subprocess.run(
            ["mmd", "-i", str(efi_img), "::EFI", "::EFI/BOOT"],
            capture_output=True, text=True, check=True,
        )
        # Copy BOOTX64.EFI
        subprocess.run(
            ["mcopy", "-i", str(efi_img), str(bootx64),
             "::EFI/BOOT/BOOTX64.EFI"],
            capture_output=True, text=True, check=True,
        )
        # Copy grub.cfg
        grub_cfg = self._staging / "boot" / "grub" / "grub.cfg"
        if grub_cfg.is_file():
            subprocess.run(
                ["mcopy", "-i", str(efi_img), str(grub_cfg),
                 "::EFI/BOOT/grub.cfg"],
                capture_output=True, text=True, check=True,
            )
        _logger.info("Built EFI image with mtools: %s", efi_img)

    def _build_efi_image_mkfs(self, efi_img: Path, bootx64: Path) -> None:
        """Build EFI image using mkfs.fat + mount (needs root)."""
        mkfs = shutil.which("mkfs.fat") or shutil.which("mkfs.vfat")
        if not mkfs:
            _logger.warning(
                "Neither mtools nor mkfs.fat found — cannot create EFI "
                "boot image. UEFI boot will not work."
            )
            return

        subprocess.run(
            [mkfs, "-F", "12", str(efi_img)],
            capture_output=True, text=True, check=True,
        )

        # We need to mount to copy files — requires root
        import tempfile

        mnt = Path(tempfile.mkdtemp(prefix="lcsas-efi-"))
        try:
            subprocess.run(
                ["mount", "-o", "loop", str(efi_img), str(mnt)],
                capture_output=True, text=True, check=True,
            )
            efi_dir = mnt / "EFI" / "BOOT"
            efi_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(bootx64), str(efi_dir / "BOOTX64.EFI"))

            grub_cfg = self._staging / "boot" / "grub" / "grub.cfg"
            if grub_cfg.is_file():
                shutil.copy2(str(grub_cfg), str(efi_dir / "grub.cfg"))

            subprocess.run(
                ["umount", str(mnt)],
                capture_output=True, text=True, check=True,
            )
            _logger.info("Built EFI image with mkfs.fat: %s", efi_img)
        except subprocess.CalledProcessError:
            _logger.warning(
                "Failed to mount EFI image — UEFI boot may not work. "
                "Install mtools for rootless EFI image creation."
            )
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(
                    ["umount", str(mnt)],
                    capture_output=True, check=False,
                )
        finally:
            mnt.rmdir()

    # ── Step 5: create ISO ───────────────────────────────────────

    def _create_iso(self) -> None:
        """Create the hybrid bootable ISO using xorriso."""
        tmp_iso = self._output.with_suffix(".iso.tmp")

        cmd = [
            self._xorriso,
            "-as", "mkisofs",
            "-r",                        # Rock Ridge
            "-J",                        # Joliet
            "-joliet-long",              # Long Joliet names
            "-iso-level", "3",           # Large file support
            "-V", self._label,           # Volume label
        ]

        # Legacy BIOS boot (El Torito) — only if isolinux.bin present
        isolinux_bin = self._staging / "isolinux" / "isolinux.bin"
        if isolinux_bin.is_file():
            cmd.extend([
                "-b", "isolinux/isolinux.bin",
                "-c", "isolinux/boot.cat",
                "-no-emul-boot",
                "-boot-load-size", "4",
                "-boot-info-table",
            ])

        # UEFI boot (El Torito alternate) — only if efiboot.img present
        efiboot_img = self._staging / "boot" / "efiboot.img"
        if efiboot_img.is_file():
            cmd.extend([
                "-eltorito-alt-boot",
                "-e", "boot/efiboot.img",
                "-no-emul-boot",
            ])

        cmd.extend([
            "-o", str(tmp_iso),
            str(self._staging),
        ])

        try:
            subprocess.run(
                cmd, capture_output=True, text=True, check=True,
            )
            os.rename(tmp_iso, self._output)
            _logger.info("Created bootable ISO: %s (%d bytes)",
                         self._output, self._output.stat().st_size)
        except subprocess.CalledProcessError as exc:
            _logger.error("xorriso failed: %s", exc.stderr)
            if tmp_iso.exists():
                tmp_iso.unlink()
            raise
        except Exception:
            if tmp_iso.exists():
                tmp_iso.unlink()
            raise

        # Make it a hybrid ISO (bootable from USB too) using isohybrid
        self._make_hybrid()

    def _make_hybrid(self) -> None:
        """Apply isohybrid MBR to allow USB boot.

        Uses ``isohybrid`` from syslinux if available.
        Not critical — ISO still works on optical media without it.
        """
        isohybrid = shutil.which("isohybrid")
        if not isohybrid:
            _logger.info(
                "isohybrid not available — ISO will work on optical but "
                "may not boot from USB. Install syslinux-utils."
            )
            return

        cmd = [isohybrid]

        # Check if UEFI mode should be enabled
        efiboot = self._staging / "boot" / "efiboot.img"
        if efiboot.is_file():
            cmd.append("--uefi")

        cmd.append(str(self._output))

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            _logger.info("Applied isohybrid MBR to %s", self._output)
        except subprocess.CalledProcessError as exc:
            _logger.warning("isohybrid failed: %s — USB boot may not work",
                            exc.stderr)

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _find_file(name: str, search_dirs: list[Path]) -> Path | None:
        """Find *name* in *search_dirs*. Returns first match or None."""
        for d in search_dirs:
            candidate = d / name
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _write_default_grub_cfg(
        path: Path,
        kernel: str = "/boot/vmlinuz",
        initrd: str = "/boot/initramfs",
    ) -> None:
        """Write a minimal GRUB configuration for the staged names."""
        path.write_text(
            "set timeout=5\n"
            "set default=0\n"
            "terminal_input console\n"
            "terminal_output console\n"
            '\nmenuentry "LCSAS Recovery" {\n'
            f"    linux {kernel} quiet loglevel=3 console=tty1\n"
            f"    initrd {initrd}\n"
            "}\n"
            '\nmenuentry "Boot from Hard Drive" {\n'
            "    insmod chain\n"
            "    set root=(hd0)\n"
            "    chainloader +1\n"
            "}\n"
        )

    @staticmethod
    def _write_default_isolinux_cfg(
        path: Path,
        kernel: str = "/boot/vmlinuz",
        initrd: str = "/boot/initramfs",
    ) -> None:
        """Write a minimal isolinux configuration for the staged names."""
        path.write_text(
            "DEFAULT lcsas\n"
            "TIMEOUT 50\n"
            "PROMPT 1\n"
            "\nLABEL lcsas\n"
            f"    KERNEL {kernel}\n"
            f"    APPEND initrd={initrd} quiet loglevel=3\n"
            "\nLABEL local\n"
            "    LOCALBOOT 0\n"
        )
