# BOOT-03: Boot menu entries point at paths the builder never stages

**Priority:** P1 · **Severity:** high · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix boot-config paths to match staged tree; drop phantom FreeBSD entries

## Problem

Even if someone built the missing kernel and initramfs tomorrow (BOOT-02), the
resulting disc still would not boot, because the boot configs and the builder
disagree about where the artifacts live. In recovery mode, `BootableISOBuilder`
stages the kernel at `/boot/vmlinuz` and the initramfs at
`/boot/initramfs.cpio.gz`, but installs the GRUB config from
`recovery/boot/efi/grub.cfg`, whose three Linux entries all load
`/boot/linux/vmlinuz` — a path that never exists on the disc, so every UEFI menu
entry 404s. The BIOS path fails differently: the builder always installs
`src/lcsas/meta/live/isolinux.cfg` (even in recovery mode), whose
`APPEND initrd=/boot/initramfs` misses the `.cpio.gz` suffix. The FreeBSD entries
chainload `/boot/freebsd/loader.bin`/`loader.efi`, artifacts that exist nowhere in
the repo. And `recovery/boot/isolinux/isolinux.cfg` — the one config whose kernel
path *does* match — is dead code (never referenced by any builder) and itself
requires `menu.c32`, which the builder never copies.

The boot path is being dropped (BOOT-01/BOOT-07), so no heir hits this today. The
finding still needs closing because the quarantined material in `experimental/boot/`
is the explicit starting point for any future revival, and a revival that begins
from configs that *look* complete but are internally inconsistent will re-ship the
defect — nothing short of an actual boot test (which doesn't exist, BOOT-08) would
catch it. The fix is to make the quarantined configs honest: correct the path
mismatches, delete the phantom FreeBSD entries and the dead config, and add an
always-on consistency test so the contract "menu entry paths == staged-tree paths"
can never silently drift again.

## Evidence

(Re-checked 2026-06-10. `recovery/boot/` paths become `experimental/boot/` after BOOT-01.)

- `src/lcsas/meta/bootable.py:156-159` — recovery mode stages
  `boot_dir / "vmlinuz"` and `boot_dir / "initramfs.cpio.gz"`.
- `src/lcsas/meta/bootable.py:172-174` — grub.cfg copied from
  `self._recovery_boot.parent / "boot" / "efi" / "grub.cfg"`.
- `recovery/boot/efi/grub.cfg:8` — `linux  /boot/linux/vmlinuz ...` (same wrong
  directory in the "shell" and "restore" entries); `:13-14` — FreeBSD entry
  `chainloader /boot/freebsd/loader.efi`.
- `ls recovery/boot/freebsd/` → only `kernel_config.txt` and `loader.conf` — no
  loader.bin/loader.efi/kernel anywhere.
- `src/lcsas/meta/bootable.py:208-210` — `_install_isolinux` always copies
  `Path(__file__).parent / "live" / "isolinux.cfg"`, both modes.
- `src/lcsas/meta/live/isolinux.cfg` — `KERNEL /boot/vmlinuz` (matches) but
  `APPEND initrd=/boot/initramfs ...` (missing `.cpio.gz` for recovery mode; the
  Alpine path stages plain `initramfs`, so the file serves two incompatible modes).
- `recovery/boot/isolinux/isolinux.cfg:9` — `UI menu.c32`;
  `bootable.py:214-235` copies only `isolinux.bin` + `ldlinux.c32`. The file is
  referenced by nothing (`grep -rn 'boot/isolinux' src/ recovery/Makefile` → no
  builder hits).
- `tests/unit/test_bootable.py` asserts grub.cfg existence/menu strings only —
  never path consistency against the staged tree.

## Fix design

Done as part of (or immediately after) the BOOT-01 quarantine move, so the
experimental material is left honest:

1. **Correct `experimental/boot/efi/grub.cfg`** — `/boot/linux/vmlinuz` →
   `/boot/vmlinuz` in all three Linux entries (matching what
   `_install_boot_files` stages). Delete the FreeBSD menuentry.
2. **Delete `experimental/boot/isolinux/isolinux.cfg`** (dead: unreferenced,
   needs an uncopied menu.c32) and delete `experimental/boot/freebsd/` (configs
   for artifacts that never existed). Record both deletions in
   `experimental/boot/README.md`'s defect index.
3. **Single isolinux source of truth.** If BOOT-07 keeps `BootableISOBuilder`
   alive anywhere: in recovery mode `_install_isolinux` must not reuse the Alpine
   `meta/live/isolinux.cfg`; generate the config from the staged filenames instead
   (extend `_write_default_isolinux_cfg` to take kernel/initrd names and make it
   the only recovery-mode source). If BOOT-07 deletes the builder, items 1-2 plus
   the parser test below are the whole fix.
4. **Builder-side validation (KEEP-variant guard, cheap either way):** in
   `BootableISOBuilder.build()`, after `_install_boot_files`/`_install_isolinux`,
   parse the *installed* `grub.cfg`/`isolinux.cfg` for `linux|initrd|KERNEL|
   APPEND initrd=` paths and raise `ValueError("boot menu references missing
   path: ...")` for any path absent from the staging tree.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_boot_config_paths.py` (new, always-on, pure
  static — survives builder deletion):
  - `test_grub_entries_reference_staged_names` — parse
    `experimental/boot/efi/grub.cfg`; every `linux`/`initrd` path must be in
    `{/boot/vmlinuz, /boot/initramfs.cpio.gz}` (the documented staged-name
    contract, asserted as constants with a comment pointing at
    `_install_boot_files`). Fails on the pre-fix `/boot/linux/vmlinuz`.
  - `test_no_phantom_artifact_references` — no config under `experimental/boot/`
    references `loader.bin`, `loader.efi`, `menu.c32`, or `/boot/freebsd/`.
- If the builder class survives (BOOT-07 decision):
  `tests/unit/test_boot_config_paths.py::test_builder_staged_tree_consistency` —
  stub kernel/initramfs at the documented names, run `_install_boot_files()` +
  `_install_isolinux()` in recovery mode, then assert every path referenced by
  the installed configs exists in the staged tree (exercises Fix 3/4).
- The only gate that would prove entries actually *load* is the QEMU boot smoke —
  recorded in BOOT-08 as the revival precondition; not built now (DROP).
- Runs via `make test-recovery-hardening` (and `make test-unit` for the builder
  variant); CI wiring per GATE-02.

## Acceptance criteria

- [ ] `grep -rn '/boot/linux/vmlinuz' experimental/ src/` → no hits.
- [ ] `experimental/boot/isolinux/isolinux.cfg` and `experimental/boot/freebsd/`
      are gone; the deletions are noted in `experimental/boot/README.md`.
- [ ] No config under `experimental/` references a FreeBSD loader or `menu.c32`.
- [ ] `pytest tests/recovery_hardening/test_boot_config_paths.py -v` passes, and
      fails when pointed at the pre-fix configs.
- [ ] (If builder kept) `BootableISOBuilder.build()` raises on a menu entry whose
      path is absent from staging, covered by the unit test.

## Dependencies & related plans

- **BOOT-01** — performs the quarantine move this plan edits inside; land first.
- **BOOT-07** — decides `BootableISOBuilder`'s fate; Fix 3/4 and the unit test
  apply only in its KEEP-the-class outcome.
- **BOOT-02** — sibling defect (initramfs content); **BOOT-08** — the boot-test
  gate that would have caught this class.

## Effort

1 day: 0.25 config corrections/deletions, 0.25 builder validation (if kept),
0.5 tests. No special environment.

---
**Implemented:** 2026-06-11. As planned (KEEP-variant: BOOT-07 undecided, builder still alive, so Fix 3/4 + builder unit tests included). Also removed the builder's dead FreeBSD staging block and reworded stale `recovery/boot/linux` kernel-copy doc lines in `experimental/boot/linux/kernel_config.*.txt` to clear the acceptance grep.
