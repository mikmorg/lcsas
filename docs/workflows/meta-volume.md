# Workflows: Meta-Volume (Bootable Disaster-Recovery Disc)

The **meta-volume** is the *entry point* for worst-case LCSAS recovery: the
machine that owned the archive is gone, the operating system is gone, the
LCSAS source tree is gone, and the only artifacts left in the world are:

1. One or more **data discs** (BD25 / MDISC100 / BDXL100 — each holographic).
2. The **meta-disc** described in this document.
3. The operator's **encryption key file** (deliberately *not* on any disc).

A meta-disc is a single bootable optical (typically BD25 or MDISC100 — the
smaller media tiers, because the rescue payload is small) that carries
*everything else needed to drive a restore*: a Linux live environment,
portable x86_64 tool binaries (`rustic`, `xorriso`, `python3`, optional
`dvdisaster`), the LCSAS Python source tree, a pure-Python `standalone_restorer.py`
fallback, the `restore_single_drive.py` stdlib helper, and the orchestrating
`restore.sh` / `restore-auto.sh` scripts. The meta-disc *deliberately omits*
`catalog.db`; see [Catalog Policy](#catalog-policy-why-no-catalogdb-on-the-meta-disc)
below.

Sibling docs: [`docs/workflows/restore-bare-metal.md`](restore-bare-metal.md),
[`docs/workflows/recovery-toolchain.md`](recovery-toolchain.md).

## Table of Contents

- [Workflow: `lcsas meta build` — produce the meta ISO](#workflow-lcsas-meta-build--produce-the-meta-iso)
- [Cross-platform recovery (Phase 21.1)](#cross-platform-recovery-phase-211)
- [Inventory: what lives on a meta-disc](#inventory-what-lives-on-a-meta-disc)
- [Workflow: Boot the meta-disc directly into restore mode](#workflow-boot-the-meta-disc-directly-into-restore-mode)
- [Workflow: Single-drive bootstrap (meta-disc occupies the only drive)](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive)
- [Catalog policy: why no `catalog.db` on the meta-disc](#catalog-policy-why-no-catalogdb-on-the-meta-disc)
- [Workflow: Refresh the meta-disc when LCSAS source changes](#workflow-refresh-the-meta-disc-when-lcsas-source-changes)
- [Variant axes summary](#variant-axes-summary)
- [Test coverage summary](#test-coverage-summary)

---

## Workflow: `lcsas meta build` — produce the meta ISO

**Purpose:** Assemble the self-contained rescue volume — bundled tools,
LCSAS source, documentation, restore scripts, optional Rustic
per-repo metadata, and (when requested) the Alpine-based live-boot
payload — into an output directory ready for ISO mastering.

**Prerequisites:**
- `rustic` and `xorriso` available on PATH (required by `_REQUIRED_TOOLS`)
  (`src/lcsas/meta/builder.py:30`).
- `python3` (its runtime is bundled too).
- Optional: `dvdisaster` on PATH (auto-bundled if present)
  (`src/lcsas/meta/builder.py:31`, `src/lcsas/meta/builder.py:1743-1746`).
- Optional `--project-root` if not auto-detecting from the installed package
  (`src/lcsas/cli/main.py:391-394`).
- Optional `--config TOML` so survivability fields populate `START_HERE.txt`,
  `KEY_INFO.txt`, and `CONFIG_SUMMARY.txt` (`src/lcsas/cli/main.py:1702-1709`,
  `src/lcsas/meta/builder.py:1987-1993`).
- Optional `--db` (or `config.db_path`) to seed per-repo Rustic metadata
  (`src/lcsas/cli/main.py:1711-1718`).
- Optional `bootable=True` + a pre-built `alpine_dir` containing `vmlinuz`,
  `initramfs`, `rootfs.squashfs` if you want a live-bootable disc
  (`src/lcsas/meta/builder.py:1694-1704`).

**Steps:**

1. CLI parses `lcsas meta build --output DIR [--project-root DIR]` and
   dispatches to `cmd_meta_build` (`src/lcsas/cli/main.py:383-394`,
   `src/lcsas/cli/main.py:1698-1742`, `src/lcsas/cli/main.py:2728`).
2. `cmd_meta_build` resolves `--output`, optional `--config`, and the
   catalog DB, then constructs `MetaVolumeBuilder`
   (`src/lcsas/cli/main.py:1711-1725`).
3. `MetaVolumeBuilder.build` creates the output directory, drops a
   `.incomplete` marker, and runs each stage in order
   (`src/lcsas/meta/builder.py:1652-1683`):
   1. `_bundle_tools` — bundle `rustic`, `xorriso`, and `python3` into
      `tools/bin/` + `tools/lib/` (and `dvdisaster` if present, plus a
      `rustic-static` glibc-independent fallback when available)
      (`src/lcsas/meta/builder.py:1730-1777`).
   2. `_bundle_source` — copy `src/` from the project root into
      `lcsas/src/` (skipping `__pycache__`, `*.pyc`, `.git`)
      (`src/lcsas/meta/builder.py:1781-1803`).
   3. `_bundle_docs` — copy `docs/`, `README.md`, and `pyproject.toml`
      (`src/lcsas/meta/builder.py:34-35`, `src/lcsas/meta/builder.py:1805-1820`).
   4. `_bundle_standalone_restorer` — generate the pure-Python single-file
      `standalone_restorer.py` at the meta root
      (`src/lcsas/meta/builder.py:1824-1836`).
   5. `_bundle_restore_helper` — copy `restore_single_drive.py` into
      `tools/` (`src/lcsas/meta/builder.py:1838-1854`).
   6. `_bundle_metadata` — if a catalog DB was provided, copy per-repo
      Rustic `config`, `keys`, `index`, and `snapshots` into
      `metadata/<repo_id>/` (`src/lcsas/meta/builder.py:1856-1899`). Note:
      this stage *does not* copy `catalog.db` — see the policy section.
   7. `_write_restore_script` / `_write_restore_auto_script` — render
      `restore.sh` and `restore-auto.sh` from the in-source
      `RESTORE_SCRIPT` / `RESTORE_AUTO_SCRIPT` constants and set them
      executable (`src/lcsas/meta/builder.py:142`,
      `src/lcsas/meta/builder.py:939`,
      `src/lcsas/meta/builder.py:1901-1911`).
   8. `_write_readme` / `_write_readme_txt` — render
      `README_RESTORE.md` and a plain-text twin
      (`src/lcsas/meta/builder.py:1384`,
      `src/lcsas/meta/builder.py:1913-1925`).
   9. `_write_volume_info` — write `volume_info.json` with `type: "meta"`,
      bundled-tool list, tool versions, and timestamps
      (`src/lcsas/meta/builder.py:1927-1977`).
   10. `_write_start_here` — write `START_HERE.txt`, `KEY_INFO.txt`,
       `CONFIG_SUMMARY.txt`, and `DISC_CARE.txt` via the
       `HolographicInjector` (`src/lcsas/meta/builder.py:1979-2032`).
   11. (Optional) `_install_live_boot` — if `bootable=True`, copy
       `vmlinuz` / `initramfs` / `rootfs.squashfs`, install GRUB EFI +
       isolinux configs, and the TUI restore wizard
       (`src/lcsas/meta/builder.py:1687-1726`,
       `src/lcsas/meta/bootable.py:109-169`).
4. The `.incomplete` marker is removed once every stage succeeds
   (`src/lcsas/meta/builder.py:1681`).
5. The output directory is then handed to xorriso (out of band; not part of
   `meta build` itself) to master into an ISO, optionally with DVDisaster
   ECC, and burned the same way as data discs.

**Expected outcome:** `args.output` contains a complete meta-volume
tree (logged contents at `src/lcsas/cli/main.py:1735-1741`), no
`.incomplete` marker, and a `volume_info.json` whose `type` field is
`"meta"`. The directory is ready to feed to `xorriso` for ISO mastering.

**Variant axes that apply:**
- **Media type:** BD25 or MDISC100 in practice — the meta payload is small
  (a few hundred MB tools + source + docs). Defined in
  `src/lcsas/config/media.py`.
- **Optical drive count:** *biggest behavior difference.* With multiple
  drives, you can leave the meta-disc in one drive and rotate data discs
  through the other(s); with a single drive, you must self-extract the
  meta-disc to RAM/disk first — see [Single-drive bootstrap](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive).
- **Recovery tier:** the meta-disc is always tier-2 cold storage; it has no
  meaningful warm-tier presence (each refresh re-bundles tools and source
  from a hot project tree).
- **Bootable vs payload-only:** `bootable=True` adds Alpine kernel +
  initramfs + GRUB/isolinux; otherwise the meta-disc is a data ISO that
  the operator mounts on an existing OS.

**Test coverage:**
- Existing: `tests/unit/test_meta_builder.py` (build creates expected
  directory layout, bundled tools function, restore.sh parses as bash,
  cascades present, START_HERE / disc-care files generated,
  `standalone_restorer.py` shipped, `restore_single_drive.py` shipped,
  single-drive defaults wired in: `test_build_creates_directory_structure`,
  `test_bundled_rustic_works`, `test_bundled_xorriso_works`,
  `test_restore_script_is_valid_bash`,
  `test_restore_script_has_cascades`, `test_start_here_generated`,
  `test_standalone_restorer_bundled`, `test_single_drive_helper_bundled`,
  `test_restore_script_single_drive_default`,
  `test_no_incomplete_marker_after_build`).
- Existing: `tests/integration/test_meta_volume_restore.py` end-to-end
  (build meta-volume, nuke everything else, restore using ONLY the
  meta-volume's bundled tools, verify byte-for-byte file hashes;
  classes `TestMetaVolumeRestore`).
- Gap: no automated test for the `bootable=True` path — Alpine artifacts
  must be supplied externally, and `_install_live_boot` is exercised only
  by inspection. Adding a fixture that fakes `vmlinuz`/`initramfs`/
  `rootfs.squashfs` would close this.
- Gap: no test that builds the meta-volume *with* `--db` populated and
  asserts that `metadata/<repo_id>/keys` is present.

**Source refs:**
- `src/lcsas/cli/main.py:376-394` (subparser),
  `src/lcsas/cli/main.py:1698-1742` (`cmd_meta_build`),
  `src/lcsas/cli/main.py:2728` (dispatch).
- `src/lcsas/meta/builder.py:1580-2032` (`MetaVolumeBuilder`).
- `src/lcsas/meta/bootable.py:109-169` (bootable-overlay helpers reused
  by `_install_live_boot`).

---

## Cross-platform recovery (Phase 21.1)

The default meta-volume bundles binaries for the host architecture
only.  Cross-platform support — the ability to recover on an ARM64
Raspberry Pi or Apple Silicon Mac without an x86_64 helper — comes
from two pieces:

1. A pinned upstream-binary cache populated by `make fetch-recovery`,
   which downloads SHA-256-verified `rustic` and `python-build-standalone`
   artifacts for every approved target (`recovery/UPSTREAM.sha256`).
2. The meta-builder's `_bundle_upstream_binaries` step
   (`src/lcsas/meta/builder.py`), which copies each cached target's
   binaries into `recovery/bin/<target>/` on the meta-volume.

Six targets are supported as of Phase 21.1 (full RFC at
[`docs/CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md)):

| Target | Notes |
|---|---|
| `x86_64-unknown-linux-musl`     | Linux x86_64, static (no host glibc dep) |
| `aarch64-unknown-linux-musl`    | Linux ARM64, static (Pi 4/5, Asahi, Graviton) |
| `armv7-unknown-linux-gnueabihf` | Linux 32-bit ARM (Pi 1/2/3/Zero) |
| `aarch64-apple-darwin`          | macOS Apple Silicon |
| `x86_64-apple-darwin`           | macOS Intel |
| `x86_64-pc-windows-gnu`         | Windows (POSIX-sh driver path) |

**Workflow:**

```bash
make fetch-recovery               # one-time ~600 MB download
lcsas meta build --output ./meta  # bundle every target with a cached binary
```

`make fetch-recovery` is idempotent — re-runs verify SHA-256 against
the pinned manifest and short-circuit when the cache is warm.  Set
`$LCSAS_RECOVERY_CACHE` to override the cache root
(default: `~/.cache/lcsas/recovery-binaries/`); air-gapped operators
can rsync the cache between hosts.

**At recovery time**, `recovery/scripts/restore.sh` auto-detects
`(uname -s, uname -m)` and picks the right `bin/<target>/` subtree.
Override with `$LCSAS_TARGET=<target-triple>` if the auto-detection
misfires (rare: chroot, foreign-arch emulator, custom uname output).
See `tests/unit/test_restore_sh_dispatcher.py` for the full matrix
of supported `(OS, machine)` pairs and the explicit rejections.

**Skipping the fetch step** is supported — the meta-volume still
builds, just with no `recovery/bin/<target>/` directories.  Single-arch
recovery on the host architecture continues to work through the legacy
`tools/bin/` bundling path.  The cross-platform path only kicks in
when the cache is populated.

### Tier-1 cross-bundling (Phase 21.10.b)

`MetaVolumeBuilder._bundle_tier1_binaries` (`src/lcsas/meta/builder.py`)
copies cross-built `lcsas-restore` binaries from the source
`recovery/bin/<short-arch>/` tree into the meta-volume's
per-target rust-triple directories.  Reached today:

| Target | Tier-1 status |
|---|---|
| `x86_64-unknown-linux-musl` | ✓ |
| `aarch64-unknown-linux-musl` | ✓ |
| `x86_64-pc-windows-gnu` | ✓ (via `zig cc`) |
| `armv7-unknown-linux-gnueabihf` | pending — Phase 21.11 |
| `aarch64-apple-darwin`, `x86_64-apple-darwin` | pending — Phase 21.12 (osxcross) |

To populate the source `recovery/bin/<short-arch>/` tree, run:

```bash
make build-recovery   # all reachable targets in one shot
# or per-target:
lcsas recovery build --arch x86_64
lcsas recovery build --arch aarch64
lcsas recovery build --arch x86_64-windows
```

Then `lcsas meta build` picks them up automatically.  Targets
without a tier-1 binary on the meta-volume fall through to tier 2
(`rustic-static`) at recovery time — restore still completes;
you just lose the "C89 + libc only" durability layer until
Phase 21.11/21.12 close the remaining gaps.  See
[`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6 Q6
for the full discussion.

**Source refs:**

- `recovery/UPSTREAM.sha256` — pinned hashes.
- `recovery/scripts/fetch_upstream.sh` — POSIX-sh downloader.
- `src/lcsas/meta/builder.py:_bundle_upstream_binaries` — per-target
  copy step (called from `_bundle_recovery_toolchain_artifacts`).
- `recovery/scripts/restore.sh` — `(uname -s, uname -m)` → `$TARGET`
  dispatcher, lines ~221-275.
- `tests/unit/test_restore_sh_dispatcher.py` — 22 dispatcher tests.
- `tests/unit/test_meta_builder.py::TestBundleUpstreamBinaries` —
  5 bundler tests.

---

## Inventory: what lives on a meta-disc

Authoritative source: `MetaVolumeBuilder.build`
(`src/lcsas/meta/builder.py:1652-1683`) and the layout comment at
`src/lcsas/meta/builder.py:1589-1601`.

| Path on disc | Origin in tree | What it is |
|---|---|---|
| `tools/bin/rustic` | system PATH, bundled by `_bundle_tools` (`src/lcsas/meta/builder.py:1740-1741`) | Rustic backup binary (dynamically linked). |
| `tools/bin/rustic-static` (optional) | `_bundle_tools` (`src/lcsas/meta/builder.py:1754-1777`) | Statically-linked rustic fallback for glibc-incompatible hosts. |
| `tools/bin/xorriso` | `_bundle_tools` (`src/lcsas/meta/builder.py:1740-1741`) | ISO authoring / extraction. |
| `tools/bin/dvdisaster` (optional) | `_bundle_tools` (`src/lcsas/meta/builder.py:1743-1746`) | RS03 ECC verifier (only bundled if found on PATH). |
| `tools/bin/python3` + `tools/lib/...` | `_bundle_tools` (`src/lcsas/meta/builder.py:1748`) | Portable CPython interpreter + stdlib + shared libs. |
| `tools/lib/python/zstandard/` | `_bundle_tools` (`src/lcsas/meta/builder.py:1750-1752`) | Optional zstd decoder for rustic v2 repos. |
| `tools/restore_single_drive.py` | `_bundle_restore_helper` (`src/lcsas/meta/builder.py:1838-1854`) | stdlib-only single-drive disc-swap helper. |
| `lcsas/src/lcsas/` | `_bundle_source` (`src/lcsas/meta/builder.py:1781-1803`) | LCSAS Python package source (no external deps). |
| `docs/`, `README.md`, `pyproject.toml` | `_bundle_docs` (`src/lcsas/meta/builder.py:1805-1820`) | Project docs including `RESTIC_FORMAT_SPEC.md` for last-resort decoding. |
| `restore.sh` | `_write_restore_script` (`src/lcsas/meta/builder.py:142`, `src/lcsas/meta/builder.py:1901-1905`) | Interactive bootstrap restore (single-drive default). |
| `restore-auto.sh` | `_write_restore_auto_script` (`src/lcsas/meta/builder.py:939`, `src/lcsas/meta/builder.py:1907-1911`) | Non-interactive scripted restore. |
| `standalone_restorer.py` | `_bundle_standalone_restorer` (`src/lcsas/meta/builder.py:1824-1836`) | Pure-Python restic decoder when no binary works. |
| `README_RESTORE.md` / `README_RESTORE.txt` | `src/lcsas/meta/builder.py:1384`, `src/lcsas/meta/builder.py:1913-1925` | Human-readable restore instructions. |
| `START_HERE.txt`, `KEY_INFO.txt`, `CONFIG_SUMMARY.txt`, `DISC_CARE.txt` | `_write_start_here` (`src/lcsas/meta/builder.py:1979-2032`) | Plain-language guidance for non-technical recovery operators. |
| `metadata/<repo_id>/{config,keys,index,snapshots}` (optional) | `_bundle_metadata` (`src/lcsas/meta/builder.py:1856-1899`) | Per-repo Rustic state that *doesn't* go stale (keys can decrypt any future pack). |
| `volume_info.json` | `_write_volume_info` (`src/lcsas/meta/builder.py:1927-1977`) | `type: "meta"` + bundled-tool inventory + tool versions. |
| `boot/vmlinuz`, `boot/initramfs`, `boot/rootfs.squashfs`, `boot/grub/grub.cfg`, `isolinux/isolinux.cfg`, `EFI/BOOT/BOOTX64.EFI` (optional) | `_install_live_boot` + `BootableISOBuilder._install_*` (`src/lcsas/meta/builder.py:1687-1726`, `src/lcsas/meta/bootable.py:109-216`) | Alpine live-boot environment + bootloaders. Only present when `bootable=True`. |
| `restore_wizard.py` (optional, with `bootable`) | `src/lcsas/meta/builder.py:1721-1726` | TUI wizard launched from the live environment. |
| **NOT present:** `catalog.db` | — | Deliberately absent — see [Catalog policy](#catalog-policy-why-no-catalogdb-on-the-meta-disc). |

The two boot configs are the entry points for the live environment:

- `src/lcsas/meta/live/grub.cfg` — UEFI boot menu. Default entry boots
  `quiet loglevel=3`; a `Verbose Boot` entry boots `loglevel=7`; entries
  also exist to chainload the local hard drive, reboot, and halt.
- `src/lcsas/meta/live/isolinux.cfg` — Legacy BIOS boot menu. Same
  entries, with banner text instructing the operator to press Enter to
  launch the restore wizard.

---

## Workflow: Boot the meta-disc directly into restore mode

**Purpose:** Recover on a machine with *no operating system at all*. The
meta-disc supplies the kernel, initramfs, root filesystem, and TUI restore
wizard.

**Prerequisites:**
- Meta-disc was built with `bootable=True` and a valid `alpine_dir`
  containing `vmlinuz`, `initramfs`, `rootfs.squashfs`
  (`src/lcsas/meta/builder.py:1694-1704`).
- x86_64 hardware capable of booting either UEFI or Legacy BIOS from
  optical media.
- An encryption key file the operator carries separately.

**Steps:**

1. Insert the meta-disc and power on the target machine. Firmware reads
   the El Torito record from the ISO.
2. **UEFI path:** the firmware loads `EFI/BOOT/BOOTX64.EFI` from the FAT
   image embedded by `_install_efi`
   (`src/lcsas/meta/bootable.py:173-216`); GRUB then reads
   `boot/grub/grub.cfg` (`src/lcsas/meta/bootable.py:121-128`,
   `src/lcsas/meta/live/grub.cfg:1-35`). Default `--id lcsas` menu entry
   boots the kernel quietly (`src/lcsas/meta/live/grub.cfg:11-14`).
3. **Legacy BIOS path:** the firmware loads `isolinux/isolinux.bin`
   installed by `_install_isolinux`
   (`src/lcsas/meta/bootable.py:132-169`), which reads
   `isolinux/isolinux.cfg` (`src/lcsas/meta/live/isolinux.cfg:1-28`).
   `DEFAULT lcsas` selects the standard recovery boot
   (`src/lcsas/meta/live/isolinux.cfg:4`,
   `src/lcsas/meta/live/isolinux.cfg:14-18`).
4. The kernel (`/boot/vmlinuz`) and initramfs (`/boot/initramfs`) come up,
   pivot into the squashfs root, and execute the live `init` script
   (`src/lcsas/meta/live/init`), which lands the operator in the
   restore TUI (`restore_wizard.py`, copied at
   `src/lcsas/meta/builder.py:1721-1726`).
5. From the wizard, the operator selects a repository / target / drive
   and the wizard invokes the same `restore.sh` flow described below.

**Expected outcome:** The machine boots without a hard disk OS and lands
the operator at the restore wizard.

**Variant axes that apply:**
- **Media type:** BD25 and MDISC100 are the practical choices.
- **Optical drive count:** *single-drive is the harder case* — see next
  workflow; with multiple drives no special bootstrap is needed.
- **Firmware:** UEFI vs BIOS — both supported, governed by the El Torito
  hybrid layout (`src/lcsas/meta/bootable.py:171-216`).
- **Recovery tier:** the meta-disc itself is tier-2 cold; the running
  live environment is ephemeral tier-1 RAM.

**Test coverage:**
- Gap: no automated test for the bootable path. The live-boot stages are
  exercised by code inspection only. Mock-Alpine-fixture tests in
  `tests/unit/test_meta_builder.py` would close this.

**Source refs:**
- `src/lcsas/meta/builder.py:1687-1726` (`_install_live_boot`).
- `src/lcsas/meta/bootable.py:109-216` (boot/EFI/isolinux installers).
- `src/lcsas/meta/live/grub.cfg`, `src/lcsas/meta/live/isolinux.cfg`,
  `src/lcsas/meta/live/init`, `src/lcsas/meta/live/restore_wizard.py`.

---

## Workflow: Single-drive bootstrap (meta-disc occupies the only drive)

**Purpose:** Handle the worst-case hardware configuration: one optical
drive, a stack of data discs, and the meta-disc. The meta-disc occupies
the drive at boot, so it must self-extract everything it needs into RAM
or disk *before* the drive is freed to accept data discs.

**Prerequisites:**
- A meta-disc (bootable or payload-only).
- An x86_64 host with at least one optical drive and enough RAM/local
  disk to hold a working copy of the meta-volume payload (a few hundred MB).
- The encryption key file mounted from external media (USB stick,
  paper-typed key, etc.).

**Steps:**

1. Boot or mount the meta-disc on the host. If it is a bootable meta-disc,
   the live environment is already running from a squashfs loaded into
   RAM by the kernel — the optical drive can be freed immediately. If it
   is a payload-only meta-disc, mount it at `/mnt/meta`.
2. **Copy the meta-volume off the disc** so the drive can be freed. The
   `README_RESTORE.md` documents this as Step 1 of single-drive mode
   (`src/lcsas/meta/builder.py:1415-1422`):
   ```
   sudo mount /dev/sr0 /mnt/meta
   cp -r /mnt/meta /tmp/lcsas-meta
   cd /tmp/lcsas-meta
   sudo umount /mnt/meta
   ```
   On a bootable meta-disc the equivalent step is performed automatically
   when the squashfs is loaded into RAM by the kernel.
3. **Eject the meta-disc.** `restore.sh` uses the optical drive as the
   data-disc loader. The script's prompt loop calls `umount_drive` +
   `eject_drive` before asking the operator to swap discs
   (`src/lcsas/meta/builder.py:458-460`).
4. **Insert any data disc** (the highest-numbered, if known, minimises
   the chance of needing a catalog upgrade —
   `src/lcsas/meta/builder.py:506-508`).
5. Run from the extracted copy:
   ```bash
   ./restore.sh --key /path/to/keyfile.txt \
                --target ~/restored/ \
                --repo REPO_NAME
   ```
   `restore.sh` defaults to single-drive mode
   (`src/lcsas/meta/builder.py:152-159`).
6. Phase 1 (bootstrap) mounts the inserted disc, reads its `catalog.db`,
   invokes `tools/restore_single_drive.py bootstrap --catalog ... --mount
   ... --cache ... --repo ...`, and emits `pick-list.json` describing
   every data disc the restore will visit
   (`src/lcsas/meta/builder.py:494-531`,
   `src/lcsas/meta/restore_single_drive.py:206-258`).
7. Phase 2 (ingest) walks the volume list. For each volume:
   - `prompt_insert "$label"` ejects the current disc and prompts for the
     wanted disc (`src/lcsas/meta/builder.py:441-488`).
   - The script then runs `restore_single_drive.py ingest --mount ...
     --cache ... --disc-label ...` which copies every needed pack into the
     cache, verifying SHA-256 on each copy
     (`src/lcsas/meta/restore_single_drive.py:300-388`).
   - Catalog upgrade: if the disc's `catalog.db` has a fresher
     `MAX(created_at)` than the bootstrap catalog, `restore.sh` re-runs
     `bootstrap --reseed` and refreshes `VOLUMES`/`PACKS_TOTAL` mid-loop
     (`src/lcsas/meta/builder.py:600-644`,
     `src/lcsas/meta/restore_single_drive.py:194-258`).
8. Phase 3 (finalize) verifies every required pack is present and intact
   in the cache. Missing packs are classified as recoverable (alternate
   disc available) or unrecoverable
   (`src/lcsas/meta/restore_single_drive.py:391-502`,
   `src/lcsas/meta/builder.py:1339-1349`).
9. After finalize succeeds, the wrapper script invokes `rustic restore`
   against the assembled cache to write files into `--target`.

**Expected outcome:** The restore completes against the assembled cache;
the optical drive holds the meta-disc only at the very start and is free
for data discs immediately afterward. State persists in
`$CACHE_DIR/restore-state.json` so an interrupted restore can be
resumed by re-running the same command
(`src/lcsas/meta/restore_single_drive.py:78-91`).

**Variant axes that apply:**
- **Media type:** BD25 or MDISC100; the data-disc media type determines
  swap count but not the bootstrap mechanism.
- **Optical drive count:** **single-drive is the only configuration where
  the bootstrap matters** — with multiple drives the meta-disc never
  needs to leave its drive.
- **Recovery tier:** the cache lives on tier-1 (warm) local disk and is
  populated from tier-2 (cold) discs.
- **Repository selection:** running without `--repo` makes
  `restore_single_drive.py bootstrap` list available repositories and
  exit 2 so the operator can pick one
  (`src/lcsas/meta/restore_single_drive.py:215-223`).

**Test coverage:**
- Existing: `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder::test_single_drive_helper_bundled`
  and `::test_restore_script_single_drive_default` (helper shipped,
  defaults wired).
- Existing: `tests/unit/test_meta_builder.py::TestSingleDriveBitsStandalone`
  (dispatcher, bash syntax, helper write).
- Existing: `tests/integration/test_meta_volume_restore.py` covers the
  *directory-mode* path end-to-end. It exercises only `--isos`, not the
  interactive single-drive prompt loop — see `test_restored_files_match_originals`
  at `tests/integration/test_meta_volume_restore.py:308-348`.
- Gap: no integration test simulates the interactive single-drive
  prompt loop with a fake drive. A test that drives `prompt_insert`
  with scripted stdin + a synthetic mountpoint would close this.
- Gap: no automated test for the catalog-upgrade path in
  `restore.sh` — `_build_pick_list` is unit-testable directly but the
  shell glue is not covered.

**Source refs:**
- `src/lcsas/meta/builder.py:142-960` (`RESTORE_SCRIPT` constant).
- `src/lcsas/meta/restore_single_drive.py` (entire file).
- `src/lcsas/meta/builder.py:1838-1854` (`_bundle_restore_helper`).

---

## Catalog policy: why no `catalog.db` on the meta-disc

**Policy:** `_bundle_metadata` deliberately copies Rustic per-repo
metadata (`config`, `keys`, `index`, `snapshots`) but **never** copies the
LCSAS catalog (`catalog.db`). The decision is documented in the
docstring of `_bundle_metadata`
(`src/lcsas/meta/builder.py:1856-1867`):

> The meta disc does NOT carry a catalog.db — it would always be stale
> (pre-dating data discs burned after the meta disc). Instead, the
> restore script bootstraps from the catalog on the first data disc the
> operator inserts, and upgrades organically when it encounters a
> fresher catalog on a later disc.
>
> We do bundle Rustic metadata (keys, config, index, snapshots) because
> keys are needed to decrypt packs and don't go stale.

**Why it works:** every data disc is *holographic* — the
`HolographicInjector` (see `staging/metadata.py` per `CLAUDE.md`'s module
map) burns a complete `catalog.db` snapshot onto every data disc at burn
time. Therefore:

1. Any data disc is sufficient to seed the restore. The bootstrap phase
   of `restore.sh` simply reads `catalog.db` off whichever disc the
   operator inserts first
   (`src/lcsas/meta/builder.py:509-515`,
   `src/lcsas/meta/restore_single_drive.py:206-258`).
2. Older discs carry older catalogs; newer discs carry newer catalogs.
   `_catalog_freshness` computes `MAX(created_at) FROM volumes` as a
   monotonic freshness token
   (`src/lcsas/meta/restore_single_drive.py:194-203`).
3. During Phase 2 (ingest), every newly-inserted disc's freshness token
   is compared against the bootstrap catalog's token. If a disc has a
   *fresher* catalog, `restore.sh` re-runs `bootstrap --reseed` to
   replace the in-cache metadata and re-emit `pick-list.json`. New
   volumes that were burned *after* the meta-disc itself appear in the
   updated pick list and get visited later in the same loop
   (`src/lcsas/meta/builder.py:600-644`,
   `src/lcsas/meta/builder.py:1289-1326`,
   `src/lcsas/meta/restore_single_drive.py:225-243`).
4. The non-interactive `restore-auto.sh` performs the same organic
   upgrade. It optionally auto-selects the *highest-labeled* `LCSAS_CD_*`
   disc first (highest label ≈ freshest catalog), which minimises the
   number of upgrades needed
   (`src/lcsas/meta/builder.py:1214-1223`,
   `src/lcsas/meta/builder.py:1289-1326`).

**Net effect:** the meta-disc never goes stale in a way that matters.
Rustic keys never go stale (a 2020-burnt meta-disc can still decrypt a
2030-burnt pack as long as the key file is the same), and the
catalog gap is bridged by the holographic copy on the freshest data
disc.

**Source refs:**
- `src/lcsas/meta/builder.py:1856-1899` (`_bundle_metadata`).
- `src/lcsas/meta/builder.py:494-498` and `src/lcsas/meta/builder.py:600-644`
  (Phase 1 + Phase 2 catalog upgrade in `RESTORE_SCRIPT`).
- `src/lcsas/meta/builder.py:1289-1326` (same upgrade in
  `RESTORE_AUTO_SCRIPT`).
- `src/lcsas/meta/restore_single_drive.py:194-258`
  (`_catalog_freshness`, `phase_bootstrap`, `--reseed`).
- `src/lcsas/meta/builder.py:1426-1430` (README explanation for
  operators).

---

## Workflow: Refresh the meta-disc when LCSAS source changes

**Purpose:** Re-mint the meta-disc so it carries the latest LCSAS code,
bumped tool versions, and (if newly-added repos exist) updated per-repo
Rustic metadata.

**Prerequisites:**
- A development checkout of LCSAS with the desired source revision.
- Same tool prerequisites as `lcsas meta build`.

**Steps:**

1. Update the LCSAS source tree (git pull, version bump, etc.) and run
   `make lint`, `make test-unit`, `make typecheck` to confirm the build
   inputs are healthy (`CLAUDE.md` commands section).
2. Run `lcsas meta build --output /tmp/meta-NEW [--config etc/lcsas.toml]
   [--db /var/lib/lcsas/archive.db]`
   (`src/lcsas/cli/main.py:383-394`). The full pipeline at
   `src/lcsas/meta/builder.py:1652-1683` regenerates `lcsas/src/` from
   the current project root (`src/lcsas/meta/builder.py:1781-1803`) and
   re-renders all in-source script constants.
3. (Optional) Build with `bootable=True` and a current `alpine_dir` if
   the live-boot environment also needs refresh.
4. Master the directory into an ISO with `xorriso` and burn it the same
   way as a data disc.
5. Record the new meta-disc as a `volumes.type='meta'` row in the
   catalog so the holographic catalog on future data discs knows about
   it. (This is a manual `INSERT` or external script — `meta build`
   itself does not write to `catalog.db`.)
6. Retire the previous meta-disc(s) per estate-planning policy
   (`docs/ESTATE_PLANNING.md`, referenced from `CLAUDE.md`).

**Expected outcome:** A new meta-disc that carries the current LCSAS
source, the same `volume_info.json` schema with a newer `created_at`,
and the same catalog policy (no `catalog.db`).

**Variant axes that apply:**
- **Media type:** unchanged — BD25/MDISC100 in practice.
- **Optical drive count:** unaffected — refresh runs on a workstation
  with whatever drive(s) are available.
- **Recovery tier:** the source artifact is hot (development tree); the
  output artifact is tier-2.
- **Cadence:** typically driven by LCSAS releases, key rotation, or
  per-disc `volume_events` review; not a per-burn-session activity.

**Test coverage:**
- Existing: `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder::test_no_pycache_in_source`
  ensures the refreshed source is clean of `__pycache__` artifacts.
- Existing: `test_no_incomplete_marker_after_build` confirms a
  successful rebuild leaves no `.incomplete` flag.
- Gap: no test exercises *re-running* `MetaVolumeBuilder.build` on an
  existing output directory to assert idempotency / overwrite
  semantics. The current code uses `shutil.rmtree` on subtrees before
  copying (`src/lcsas/meta/builder.py:1789-1800`,
  `src/lcsas/meta/builder.py:1811-1817`) but no test asserts the
  invariants.
- Gap: no policy enforcement for registering the new meta-volume in
  `catalog.db`; this is operator discipline today.

**Source refs:**
- `src/lcsas/cli/main.py:1698-1742` (CLI driver — same path as a fresh
  build).
- `src/lcsas/meta/builder.py:1652-1683` (build orchestration).
- `src/lcsas/meta/builder.py:1781-1820` (source + docs copy).

---

## Variant axes summary

| Axis | Values | Where it matters |
|---|---|---|
| Media type | BD25, MDISC100 (typical), TEST_TINY (CI) | `lcsas meta build` output size and mastering choice (`src/lcsas/config/media.py`). |
| Optical drive count | 1 (must self-extract before swap) vs ≥2 (meta-disc stays loaded) | **Biggest behavior difference** — see [Single-drive bootstrap](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive). |
| Recovery tier | Tier-2 cold (the disc itself), Tier-1 warm (extracted copy + cache), Tier-0 ephemeral (live boot RAM) | Drives how the operator stages tools before swapping discs. |
| Firmware | UEFI vs Legacy BIOS | Bootable variant only; both supported via El Torito hybrid (`src/lcsas/meta/bootable.py:171-216`). |
| Bootable flag | `bootable=False` (default, payload-only) vs `bootable=True` (live boot) | `src/lcsas/meta/builder.py:1687-1726`. |
| Repository selection at restore | `--repo NAME` vs "list available and exit" | `src/lcsas/meta/restore_single_drive.py:215-223`. |
| Catalog freshness | Bootstrap-from-first-disc vs organic upgrade on fresher discs | `src/lcsas/meta/restore_single_drive.py:194-258`, `src/lcsas/meta/builder.py:600-644`. |

---

## Test coverage summary

| Workflow | Unit | Integration | Gaps |
|---|---|---|---|
| `lcsas meta build` (payload) | `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder` | `tests/integration/test_meta_volume_restore.py` | No test for `--db`-populated `metadata/` output. |
| `lcsas meta build` (bootable) | — | — | No fixture mocking Alpine artifacts. |
| Meta-disc inventory | `test_build_creates_directory_structure`, `test_bundled_*_works`, `test_start_here_generated`, `test_standalone_restorer_bundled`, `test_single_drive_helper_bundled` | `TestMetaVolumeRestore::test_meta_volume_has_all_tools`, `::test_meta_volume_has_source`, `::test_meta_volume_has_docs` | None significant. |
| Boot-from-disc | — | — | No mocked boot test for `_install_live_boot`. |
| Single-drive bootstrap | `test_single_drive_helper_bundled`, `test_restore_script_single_drive_default`, `TestSingleDriveBitsStandalone::test_restore_script_constant_has_single_drive_dispatch`, `::test_restore_script_passes_bash_syntax` | `TestMetaVolumeRestore::test_restore_sh_executes` (directory mode only) | No interactive prompt-loop simulation; no shell-level catalog-upgrade test. |
| Catalog policy | Code inspection | `TestMetaVolumeRestore` exercises the holographic catalog implicitly (each data disc carries `catalog.db`). | No direct test for `_catalog_freshness` precedence / `--reseed` behavior. Unit tests against `restore_single_drive.phase_bootstrap` with a synthetic catalog would close this. |
| Source refresh | `test_no_pycache_in_source`, `test_no_incomplete_marker_after_build` | — | No idempotency / re-run test on an existing output directory. |
