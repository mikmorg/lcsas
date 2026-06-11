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
| BOOT-06 | `lcsas-init` probes optical nodes only (`/dev/sr0-3`, `/dev/cdrom`, `/dev/dvd`), so a USB-booted image cannot find itself — even though the builder runs `isohybrid --uefi` precisely to make USB boot work. See the sentinel-scan fix spec below. `recovery/src/lcsas-init/` and its built binaries remain under `recovery/` — inert without a kernel. |

## BOOT-06: sentinel-scan fix spec (recorded, not implemented)

Booted from a USB stick (the realistic medium for 2035+ machines without
optical drives), the recovery medium appears as `/dev/sdX` — a node
`try_discs()` in `recovery/src/lcsas-init/init.c` never probes — so init
logs "no optical disc found; dropping to shell" and a non-technical
operator dead-ends at a bare BusyBox prompt. Any revival of the boot
path MUST implement the following instead of re-deriving it:

* **Probe set** — extend `try_discs()` beyond the optical nodes to
  `/dev/sd[a-z]` plus partitions 1–4, `/dev/vd[a-z]`, and
  `/dev/nvme[0-3]n1` plus partitions `p1`–`p4`.
* **Identify by sentinel, not device class** — after each successful
  read-only mount (iso9660 then udf, as today), accept the medium only
  if `access("/mnt/recovery/scripts/restore.sh", R_OK) == 0`; unmount
  non-matches and keep scanning. Sentinel selection mirrors
  `meta_disc_is_live()` in lcsas-restore's disc locator
  (`recovery/src/lcsas-restore/disc_locator.c`).
* **Required tests for revival** (gated with the BOOT-08 boot smoke):
  1. `recovery/tests/test_init_medium_scan.c` — candidate/sentinel logic
     against a fake `/dev` tree, in the style of
     `recovery/tests/test_disc_locator.c`.
  2. A USB-attach leg in the BOOT-08 QEMU boot smoke: the same image
     attached as `usb-storage` instead of `-cdrom`, asserting the same
     serial handoff marker.

This record is pinned by
`tests/recovery_hardening/test_boot_path_quarantined.py`.

## Contents

* `linux/` — Linux 6.6 LTS kernel config notes (x86_64 / aarch64 / riscv64)
  and kernel cmdline.
* `efi/` — UEFI (GRUB) boot menu config.
* `initramfs/` — manifest + deterministic cpio.gz assembly script.
* `bootable.py` — the bootable-ISO builder (`BootableISOBuilder`), moved
  out of `src/lcsas/meta/` by BOOT-07 (which also deleted its Alpine
  live mode together with `src/lcsas/meta/live/`: the Alpine rootfs was
  assembled from unpinned packages fetched over the network at build
  time).  Only the `recovery_boot_dir` mode targeting this directory
  survives; exercised by `tests/unit/test_boot_config_paths.py`
  (skip-if-absent).
