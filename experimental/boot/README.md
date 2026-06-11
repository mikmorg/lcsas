# experimental/boot/ — quarantined bootable-media scaffolding

> **STATUS: NOT BOOTABLE / NEVER BUILT.**
> **Dropped 2026-06 per the deep audit (plan BOOT-01).**
>
> No disc any LCSAS build has ever produced is bootable. This directory
> holds the config-only scaffolding for the abandoned bootable meta-volume
> path, quarantined out of `recovery/` so it no longer ships on burned
> discs (`MetaVolumeBuilder` copies the whole `recovery/` tree onto every
> meta disc) and no longer appears in `recovery/MANIFEST.sha256`.
>
> **Revival precondition:** an automated QEMU/OVMF boot smoke gate that
> proves a built image actually boots, as specified in plan BOOT-08
> (`plans/audit-2026-06/BOOT-08-*.md`). Do not move anything back under
> `recovery/` without that gate in CI.

## Why it was dropped

* The path was unreachable scaffolding: `lcsas meta build` exposes only
  `--output` / `--project-root`; `MetaVolumeBuilder(bootable=...)` defaults
  to `False` and no caller ever set it; the `--recovery-boot` flag that
  `recovery/docs/BOOT.txt` used to document never existed in the argparse
  tree.
* No kernel, bootloader, initramfs, or BusyBox binary exists anywhere in
  the repository — this directory contains only config text files and an
  assembly script.
* Even fully built, a self-made boot stack fights decades of hardware
  evolution (Secure Boot, ARM-only machines, driver decay — see BOOT-04).
  A current signed live-Linux image solves all of that for free, so the
  no-OS recovery journey now routes to a live-USB procedure
  (`recovery/docs/BOOT.txt`, `docs/workflows/restore-live-usb.md`).

## Known defects in this material (indexed, intentionally unfixed here)

| Plan | Defect |
|------|--------|
| BOOT-02 | `initramfs/build_initramfs.sh` silently emits zero-byte placeholder files for missing sources (e.g. BusyBox, kernel), so a "successful" build produces a non-booting initramfs. |
| BOOT-03 | **Fixed 2026-06.** Boot menu entries loaded the kernel from a `/boot/linux/` subdirectory the builder never stages (the kernel is staged at `/boot/vmlinuz`). `efi/grub.cfg` now uses the staged names (pinned by `tests/recovery_hardening/test_boot_config_paths.py`). Deleted: `isolinux/isolinux.cfg` (dead — referenced by no builder, required a `menu.c32` nothing copies) and `freebsd/` (the FreeBSD menu entries chainloaded `loader.bin`/`loader.efi`, artifacts that never existed anywhere in the repo). |
| BOOT-05 | The boot flow targets a tmpfs restore destination, which cannot hold a real archive. |
| BOOT-06 | `lcsas-init` scans optical devices only (`/dev/sr*`), so a USB-stick boot of the same image would never find its medium. `recovery/src/lcsas-init/` and its built binaries remain under `recovery/` for now — inert without a kernel. |

## Contents

* `linux/` — Linux 6.6 LTS kernel config notes (x86_64 / aarch64 / riscv64)
  and kernel cmdline.
* `efi/` — UEFI (GRUB) boot menu config.
* `initramfs/` — manifest + deterministic cpio.gz assembly script.
