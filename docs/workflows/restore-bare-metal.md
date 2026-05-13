# Workflows: Bare-Metal Recovery (initramfs / Live USB)

This document covers the *house-on-fire* recovery path: the inheritor (or the original owner after a total system loss) has the LCSAS optical discs in hand but **no working operating system, no Python interpreter, no internet, and possibly no second computer**. Recovery happens by booting the LCSAS meta-disc directly. PID 1 is a 158-line statically-linked C program (`recovery/src/lcsas-init/init.c`); everything from there forward is C and POSIX shell only — Python is explicitly **off** the bare path.

This is the worst credible failure mode the toolchain is designed to survive. Every step has been audited so that an inheritor in 2045 with a stack of BD-R discs, a USB Blu-ray drive, and any x86_64 PC can recover the data without acquiring or trusting any third-party software. Sibling docs cover gentler scenarios: `docs/workflows/restore-host-linux.md` (working Linux host), `docs/workflows/restore-windows.md` (working Windows host), `docs/workflows/restore-disc-only.md` (single-disc spot recovery), `docs/workflows/recovery-toolchain.md` (binary cascade architecture), and `docs/workflows/meta-volume.md` (how the meta-disc is built).

The bare-metal path uses tiers 1–2 of the C/Rust cascade defined in `recovery/scripts/restore.sh:8`–`recovery/scripts/restore.sh:12`. Tier 3 (Python) is reachable from a booted live env if every C and Rust option failed, but the canonical bare-metal recovery never executes a Python interpreter.

## Table of contents

- [Boot the meta-disc directly into the initramfs](#boot-the-meta-disc-directly-into-the-initramfs)
- [Boot from a recovery USB created from the meta-disc](#boot-from-a-recovery-usb-created-from-the-meta-disc)
- [Multi-disc swap (two or more optical drives)](#multi-disc-swap-two-or-more-optical-drives)
- [Single-drive RAM relocation (one optical drive only)](#single-drive-ram-relocation-one-optical-drive-only)
- [The `restore.sh` orchestrator inside the live env](#the-restoresh-orchestrator-inside-the-live-env)
- [Fallback: `restore_auto.sh` (non-interactive multi-repo restore)](#fallback-restore_autosh-non-interactive-multi-repo-restore)
- [Notes & gaps](#notes--gaps)

---

## Boot the meta-disc directly into the initramfs

**Purpose:** Power on a machine with no installed OS, insert the LCSAS meta-disc, and have the recovery toolchain running in a Linux live environment within seconds — no installer, no network, no host filesystem.

**Prerequisites:**
- The LCSAS **meta-disc** (a BD-R, DVD, or MDISC burned by `lcsas meta build`, identifiable by the `LCSAS_META` volume label).
- An x86_64, aarch64, or riscv64 machine with either a BIOS or UEFI firmware capable of booting from optical media. (See `recovery/docs/BOOT.txt:12`.)
- An optical drive — internal or USB. **Drive count matters** for the swap UX; see the single-drive variant below.
- The repository password — there is no recovery from password loss (`recovery/docs/RECOVER.txt:130`).
- A target writable medium (SSD, HDD, USB) large enough to hold the restored snapshot, mounted before the restore begins **or** addressable as `/tmp/restored` inside the live env (tmpfs, RAM-only) for small recoveries.

**Steps:**
1. Insert the meta-disc and set firmware to boot from optical. Firmware presents the disc's BIOS or UEFI bootloader (`recovery/docs/BOOT.txt:10`).
2. The bootloader displays four menu entries; the default (10 s timeout) is "Linux 6.6 LTS (recovery)" (`recovery/docs/BOOT.txt:18`). Entries 2–4 are FreeBSD, raw BusyBox, and direct `lcsas-restore` invocation respectively.
3. The selected entry loads `/boot/linux/vmlinuz` plus `/boot/initramfs.cpio.gz` (`recovery/docs/BOOT.txt:25`). The cpio is built from `recovery/boot/initramfs/manifest.txt` and contains `/init` plus a BusyBox toolbox (`recovery/boot/initramfs/manifest.txt:28`–`recovery/boot/initramfs/manifest.txt:51`).
4. The kernel exec's `/init`, which is the statically-linked `lcsas-init` binary (`recovery/src/lcsas-init/init.c:88`). As PID 1 it:
   - Mounts `/proc`, `/sys`, and `/dev` (`recovery/src/lcsas-init/init.c:96`–`recovery/src/lcsas-init/init.c:107`).
   - Mounts tmpfs at `/run` (64 MiB) and `/tmp` (256 MiB) (`recovery/src/lcsas-init/init.c:108`–`recovery/src/lcsas-init/init.c:111`).
   - Probes `/dev/sr0`–`/dev/sr3`, `/dev/cdrom`, `/dev/dvd` for an ISO 9660 (and then UDF) filesystem and mounts the first hit read-only at `/mnt` (`recovery/src/lcsas-init/init.c:55`–`recovery/src/lcsas-init/init.c:78`).
   - On failure, drops to a BusyBox shell so a savvy user can mount the disc manually (`recovery/src/lcsas-init/init.c:115`–`recovery/src/lcsas-init/init.c:121`).
5. If `/mnt/recovery/scripts/restore.sh` exists, `lcsas-init` sets `LCSAS_META_DISC=/mnt` (the *exclusion path* hint for single-drive relocation), `chdir("/")` to avoid pinning the disc through the `exec` barrier, and then `execl`s BusyBox sh on `restore.sh` with `/mnt/recovery` as RECOVERY_ROOT and `/tmp/restored` as TARGET (`recovery/src/lcsas-init/init.c:124`–`recovery/src/lcsas-init/init.c:143`).
6. `restore.sh` performs its own single-drive relocation check (see the dedicated section below) and then runs through tiers 1–2, exec'ing the chosen recovery binary against `RECOVERY/repo` (`recovery/scripts/restore.sh:360`–`recovery/scripts/restore.sh:380`).

**Expected outcome:**
- The live Linux kernel + initramfs are running entirely in RAM; the meta-disc is mounted RO at `/mnt`.
- `lcsas-restore` (tier 1 prebuilt static binary) is decrypting and writing files to `/tmp/restored`, prompting on stdin for additional data discs as needed (multi-disc UX, see below).
- The meta-disc's only consumers post-relocation are short-lived `open`/`close` cycles in `lcsas_repo_read_blob` (audited in `recovery/docs/MULTI_DISC_DESIGN.txt:493`–`recovery/docs/MULTI_DISC_DESIGN.txt:510`).
- Exit code 0 from `restore.sh` indicates a complete restore; the user can `cp -r /tmp/restored /mnt/<persistent>` to copy out, or `dd if=/dev/zero of=...` to wipe, before powering off.

**Variant axes that apply:**
- Media type: meta-disc is typically BD25 or MDISC100; the boot stack also works on DVD / smaller test media. Booted-from data discs (data-only volumes are *not* bootable) cannot enter this path.
- OS: always *Linux bare-metal initramfs* (kernel 6.6 LTS per `recovery/docs/BOOT.txt:25`). FreeBSD entry exists but uses the same `lcsas-init` -> `restore.sh` handoff (`recovery/docs/BOOT.txt:35`).
- Optical drive count: single-drive is the dominant case for modern laptops; multi-drive is desktop / dedicated recovery workstation territory. The single-drive sub-workflow has materially different UX.
- Recovery tier: tier 1 (prebuilt `lcsas-restore`) on the common path; tier 2 (`rustic-static`) is the cross-check. Tier 3 (pure-Python) sits below as the last resort.

**Test coverage:**
- Existing:
  - `recovery/tests/test_bare_path.sh` — statically proves tiers 1–2 contain zero Python references, then runs the cascade under a PATH that shims out `python*` and `LCSAS_ALLOW_PYTHON_TIER=0` to prove the bare path is binary-only (`recovery/tests/test_bare_path.sh:22`–`recovery/tests/test_bare_path.sh:155`).
  - `recovery/tests/test_e2e.py::main` — builds a synthetic restic v1 repo with the Python crypto helpers and verifies `lcsas-restore` recovers it byte-for-byte (`recovery/tests/test_e2e.py:277`).
- Gaps:
  - No automated test boots the actual `vmlinuz` + `initramfs.cpio.gz` under QEMU; `lcsas-init`'s mount logic is exercised only indirectly. The `try_discs` loop and the `LCSAS_META_DISC=/mnt` setenv handoff (`recovery/src/lcsas-init/init.c:55`, `recovery/src/lcsas-init/init.c:128`) are not unit-tested.
  - No regression test covers the BusyBox-shell fallback at `recovery/src/lcsas-init/init.c:117` or the per-row error-log branches in `try_mount`.
  - FreeBSD alternate boot entry (`recovery/docs/BOOT.txt:30`) has no E2E test in the repo.

**Source refs:**
- Boot stack: `recovery/docs/BOOT.txt:10`–`recovery/docs/BOOT.txt:53`
- Initramfs manifest: `recovery/boot/initramfs/manifest.txt:28`–`recovery/boot/initramfs/manifest.txt:51`
- Initramfs builder: `recovery/boot/initramfs/build_initramfs.sh:1`–`recovery/boot/initramfs/build_initramfs.sh:69`
- PID 1: `recovery/src/lcsas-init/init.c:88`–`recovery/src/lcsas-init/init.c:157`
- Disc detection: `recovery/src/lcsas-init/init.c:55`–`recovery/src/lcsas-init/init.c:78`
- Handoff env vars: `recovery/src/lcsas-init/init.c:124`–`recovery/src/lcsas-init/init.c:143`

---

## Boot from a recovery USB created from the meta-disc

**Purpose:** When the inheritor's machine has no optical drive at all but does have USB ports, the meta-disc's contents can be copied bit-for-bit onto a USB stick that the firmware will boot exactly the same way.

**Prerequisites:**
- The meta-disc's ISO image, accessible from a working machine (`dd if=/dev/sr0 of=meta.iso bs=2M` on any Linux/macOS box, or use any ISO-extraction tool on Windows).
- A USB stick at least as large as the meta-disc (typically ≥ 4 GiB to comfortably fit the kernel + initramfs + binaries + source tree).
- Firmware that can boot from USB (true of every PC since ~2010).
- All other prerequisites from the previous workflow (password, target storage).

**Steps:**
1. From any working host, write the meta-disc ISO to the USB stick with `dd if=meta.iso of=/dev/sdX bs=2M conv=fsync` (Linux/macOS) or Rufus / `dd-for-windows` / similar (Windows). The meta-volume's xorriso recipe produces an isohybrid image so the same bytes are valid for both BIOS/MBR and UEFI/GPT booting (`recovery/docs/BOOT.txt:10`–`recovery/docs/BOOT.txt:14`).
2. Insert the USB stick into the target machine and select USB boot in the firmware menu.
3. Bootloader and kernel load identically to the optical path; the only difference is that the kernel sees `/dev/sda` (or `/dev/sdb`) rather than `/dev/sr0`.
4. `lcsas-init` runs `try_discs("/mnt")` (`recovery/src/lcsas-init/init.c:115`). The hard-coded candidate list is `/dev/sr0`–`/dev/sr3`, `/dev/cdrom`, `/dev/dvd` (`recovery/src/lcsas-init/init.c:57`–`recovery/src/lcsas-init/init.c:61`) — **none of these match a USB stick**. The first call returns -1 and `lcsas-init` drops to the BusyBox shell (`recovery/src/lcsas-init/init.c:116`).
5. From the BusyBox shell, the user manually mounts the USB stick: `mount -o ro /dev/sda1 /mnt` (the partition may be `/dev/sda`, `/dev/sdb1`, etc. — discoverable by `cat /proc/partitions`).
6. The user then invokes the orchestrator directly:
   ```
   export LCSAS_META_DISC=/mnt
   sh /mnt/recovery/scripts/restore.sh /mnt/recovery /tmp/restored latest
   ```
   This is the same exec line `lcsas-init` would have used (`recovery/src/lcsas-init/init.c:136`–`recovery/src/lcsas-init/init.c:139`).

**Expected outcome:**
- Live env up; recovery proceeds as in the meta-disc workflow.
- USB sticks are random-access so single-drive constraints do **not** apply — the USB can stay mounted while additional optical discs are inserted/ejected from the optical drive.

**Variant axes that apply:**
- Media type: USB stick (FAT32 / ISO 9660 / UDF, whatever `dd` produced).
- OS: *Linux live USB* (same kernel + initramfs as the meta-disc path).
- Optical drive count: data discs still need optical access, but the meta-disc lookup is irrelevant on USB — single-drive constraints relax.
- Recovery tier: tier 1, identical to the optical path.

**Test coverage:**
- Existing: none. The USB path is not exercised by any test in `recovery/tests/`.
- Gaps:
  - `lcsas-init`'s device-candidate list at `recovery/src/lcsas-init/init.c:57` does not include `/dev/sd*` or `/dev/nvme*`, so USB / SSD recovery media require manual mount. A future change could probe block devices via `/sys/block` and try each — that would make USB booting a turnkey experience instead of "drop to shell, then mount manually".
  - No test confirms the isohybrid image produced by `lcsas meta build` actually boots on UEFI USB.
  - `start_here.txt` style on-disc documentation for the manual-mount step does not exist; the inheritor has to know `mount` syntax.

**Source refs:**
- Device candidate list (where the gap lives): `recovery/src/lcsas-init/init.c:57`–`recovery/src/lcsas-init/init.c:61`
- BusyBox shell fallback: `recovery/src/lcsas-init/init.c:117`
- Manual handoff to `restore.sh` (mirrors `lcsas-init`'s execl): `recovery/src/lcsas-init/init.c:136`–`recovery/src/lcsas-init/init.c:139`
- Boot stack (same for optical and USB): `recovery/docs/BOOT.txt:10`–`recovery/docs/BOOT.txt:36`

---

## Multi-disc swap (two or more optical drives)

**Purpose:** When the LCSAS archive spans multiple data volumes (the normal case — a 1 TiB dedup'd archive typically lives on 4–50 data discs per `recovery/docs/MULTI_DISC_DESIGN.txt:17`), the recovery tool must read packs from each disc in turn. With **two or more drives**, all discs can be inserted simultaneously and the orchestrator passes every mount point to the recovery binary, eliminating user prompts for the common case.

**Prerequisites:**
- Booted recovery environment (initramfs or live USB) per one of the workflows above.
- Two or more optical drives — either internal SATA/IDE drives, USB external drives, or a mix.
- All data discs available physically; the user inserts as many as fit simultaneously.

**Steps:**
1. After `lcsas-init` mounts the meta-disc at `/mnt`, the user inserts each data disc into the additional drives. The kernel/udev auto-creates `/dev/sr1`, `/dev/sr2`, etc. but does **not** auto-mount them — the user mounts each manually from the BusyBox shell (or relies on the `lcsas-init` path which doesn't, so this is typically done after dropping to the prompt that follows the meta-disc handoff if more discs need mounting).
   - In practice the user runs `mount -o ro /dev/sr1 /media/disc_a`, `mount -o ro /dev/sr2 /media/disc_b`, etc.
2. `restore.sh` auto-discovers every mounted disc that looks like an LCSAS volume. It scans `/Volumes/*` (macOS hosts only — not on initramfs but covered for completeness), `/media/$USER/*`, `/media/*`, and `/mnt/*` (`recovery/scripts/restore.sh:287`–`recovery/scripts/restore.sh:304`).
3. For each candidate it calls `add_pack_search` (`recovery/scripts/restore.sh:267`–`recovery/scripts/restore.sh:285`):
   - Skips anything under `META_DISC` (avoids re-locking the meta-disc when a single drive is in use — important even with multiple drives, see edge cases).
   - Adds `$mnt` to `--pack-search` if `$mnt/data` exists.
   - Adds `$mnt/repo` if `$mnt/repo/data` exists.
4. The orchestrator picks the *freshest* `catalog.db` it can find across all currently-mounted discs by mtime (`recovery/scripts/restore.sh:321`–`recovery/scripts/restore.sh:358`) — important because the meta-disc deliberately ships **without** a `catalog.db` (it would always be stale at burn time, per `recovery/scripts/restore.sh:316`–`recovery/scripts/restore.sh:319` and `recovery/docs/MULTI_DISC_DESIGN.txt:296`).
5. The chosen `lcsas-restore` (tier 1) is exec'd with `--pack-search` flags for every mounted volume and `--catalog <freshest>` (`recovery/scripts/restore.sh:368`–`recovery/scripts/restore.sh:370`).
6. If any pack is still missing — e.g. the archive spans 8 discs but the user only mounted 6 — `lcsas-restore`'s `disc_locator` consults the catalog and prints a clear prompt naming the missing volume's label, then reads stdin for the user to insert the disc and press ENTER (design at `recovery/docs/MULTI_DISC_DESIGN.txt:39`–`recovery/docs/MULTI_DISC_DESIGN.txt:75`; on-disc text at `recovery/docs/RECOVER.txt:171`–`recovery/docs/RECOVER.txt:180`).
7. On user input the binary re-scans every search path plus the standard mount-point parents and resumes restore from the missing-pack boundary; no progress is lost.

**Expected outcome:**
- Common case (all needed discs already mounted): zero prompts, full restore exits 0.
- Less common case (user inserts discs reactively): each prompt names exactly which volume label is needed and which discs are currently visible; the user is never told only "pack not found: <hash>" as in the broken pre-MVP behavior (`recovery/docs/MULTI_DISC_DESIGN.txt:26`–`recovery/docs/MULTI_DISC_DESIGN.txt:32`).

**Variant axes that apply:**
- Media type: any optical media (BD25, MDISC100, DVD, test sizes).
- OS: *Linux bare-metal initramfs* (or *Linux live USB*).
- Optical drive count: ≥ 2 — that's what makes this the simple path.
- Recovery tier: tier 1 (uses the in-binary `disc_locator` module). Tier 2 (`rustic-static`) takes over if Tier 1 fails; tier 3 (pure-Python) sits below as the last resort.

**Test coverage:**
- Existing:
  - `recovery/tests/test_multidisc.py::case_both_visible` — both pack dirs reachable via `--pack-search`, non-interactive success (`recovery/tests/test_multidisc.py:98`).
  - `recovery/tests/test_multidisc.py::case_fail_fast` — one pack dir missing, `--interactive off`, asserts the binary fails fast (`recovery/tests/test_multidisc.py:132`).
  - `recovery/tests/test_multidisc.py::case_interactive_swap` — delayed swap via stdin pipe, asserts recovery completes after prompt + retry (`recovery/tests/test_multidisc.py:167`).
  - `recovery/tests/test_multidisc.py::case_catalog_freshest_pick` — proves the mtime-based freshest-catalog selector (`recovery/tests/test_multidisc.py:446`).
  - `recovery/tests/test_multidisc.py::case_catalog_prompt_label` — proves the prompt text mentions the catalog's volume label (`recovery/tests/test_multidisc.py:524`).
- Gaps:
  - No test covers > 2 simultaneously mounted discs.
  - Auto-mount on disc insertion (the inotify/`WM_DEVICECHANGE` extension parked in `recovery/docs/MULTI_DISC_DESIGN.txt:69`–`recovery/docs/MULTI_DISC_DESIGN.txt:76`) is unimplemented; the prompt loop is strictly Enter-driven.
  - The interaction between `restore.sh`'s auto-discovery and a partial mount (one disc mounted, others not yet) is exercised only by the catalog-freshest test, not specifically asserted.

**Source refs:**
- Pack search auto-discovery: `recovery/scripts/restore.sh:267`–`recovery/scripts/restore.sh:304`
- Catalog freshest-pick: `recovery/scripts/restore.sh:321`–`recovery/scripts/restore.sh:358`
- Exec with multi-disc args: `recovery/scripts/restore.sh:362`–`recovery/scripts/restore.sh:370`
- Design doc: `recovery/docs/MULTI_DISC_DESIGN.txt:11`–`recovery/docs/MULTI_DISC_DESIGN.txt:75`
- Pack -> volume map sources (redundancy): `recovery/docs/MULTI_DISC_DESIGN.txt:251`–`recovery/docs/MULTI_DISC_DESIGN.txt:286`

---

## Single-drive RAM relocation (one optical drive only)

**Purpose:** Solve the *meta-disc-held-captive* problem. A user with only one optical drive cannot simply eject the meta-disc and insert a data disc while the recovery script is running off the meta-disc — `sh` and `lcsas-restore` would hold open file descriptors on the read-only mount. LCSAS releases the drive *before* the first prompt by copying the script and the binary tree into RAM, re-exec'ing from there, and then explicitly closing every metadata fd in the binary before the locator blocks for user input. This is the single most user-facing piece of the bare-metal path because optical drives are increasingly rare on modern hardware (`recovery/docs/UX_CONCERNS.txt:137`) — most inheritors will have exactly one external USB BD reader.

This sub-workflow is materially different from the multi-drive case and is the **default assumption** of the bare-metal path.

**Prerequisites:**
- Booted recovery env per the meta-disc or USB workflow.
- **Exactly one** optical drive (or any setup where the meta-disc and a data disc cannot be inserted simultaneously).
- The meta-disc currently inserted.
- A writable scratch directory with enough RAM to hold the recovery binaries and scripts (~10 MiB typical). `/tmp` (256 MiB tmpfs from `lcsas-init`, per `recovery/src/lcsas-init/init.c:111`) is the default target.

**Steps:**
1. `lcsas-init` sets `LCSAS_META_DISC=/mnt` and exec's `restore.sh` (`recovery/src/lcsas-init/init.c:128`–`recovery/src/lcsas-init/init.c:143`). This explicit hint matters: `restore.sh`'s probe loop is conservative and a misclassification here would hold the disc.
2. `restore.sh` enters the relocation check at the top of its main body (`recovery/scripts/restore.sh:129`–`recovery/scripts/restore.sh:160`):
   - Skips entirely if `LCSAS_RELOCATED` is already set (we are the in-RAM copy) or `LCSAS_NO_RELOCATE=1` (dev/test override).
   - Strongest signal: `LCSAS_META_DISC` is set **and** `SCRIPT_DIR` is inside it (`recovery/scripts/restore.sh:133`–`recovery/scripts/restore.sh:137`). This is always true under `lcsas-init`.
   - Fallback signal: `SCRIPT_DIR` is not writable (`recovery/scripts/restore.sh:139`).
   - Further fallback: `findmnt` reports the filesystem as `iso9660`, `udf`, `squashfs`, `cramfs`, or `romfs` (`recovery/scripts/restore.sh:142`–`recovery/scripts/restore.sh:147`).
3. If relocation is needed, `restore.sh` calls `relocate_to_ram` (`recovery/scripts/restore.sh:70`–`recovery/scripts/restore.sh:124`):
   - Picks the first writable directory among `$TMPDIR`, `$XDG_RUNTIME_DIR`, `/tmp`, `/run`, `/var/tmp` that is **not** inside the meta-disc mount (`recovery/scripts/restore.sh:75`–`recovery/scripts/restore.sh:83`).
   - `mktemp -d "$cand/lcsas-restore.XXXXXX"` creates the scratch dir.
   - Mirrors the on-disc layout under the scratch dir: `cp -f` for `restore.sh` and `detect_arch.sh`, `cp -R` for the entire `bin/` tree, and a best-effort copy of any nearby `catalog.db` (`recovery/scripts/restore.sh:92`–`recovery/scripts/restore.sh:112`).
   - Prints a "you may eject the recovery disc when the binary prompts for a data disc" line to stderr (`recovery/scripts/restore.sh:114`–`recovery/scripts/restore.sh:116`).
   - Sets `LCSAS_RELOCATED=<orig meta mount>`, `cd /`, then `exec "$ramdir/recovery/scripts/restore.sh" "$@"` — the re-exec'd `sh` inherits the new cwd and the new script path, dropping every fd into the meta-disc (`recovery/scripts/restore.sh:118`–`recovery/scripts/restore.sh:123`).
4. The relocated `restore.sh` re-enters at line 1 with `LCSAS_RELOCATED` set, so the relocation block short-circuits.
5. `META_DISC` is computed from `LCSAS_RELOCATED` (or `LCSAS_META_DISC` if set, e.g. on macOS host) so downstream consumers know which path to exclude (`recovery/scripts/restore.sh:165`).
6. `add_pack_search` excludes any candidate under `META_DISC` from `--pack-search` (`recovery/scripts/restore.sh:271`–`recovery/scripts/restore.sh:274`). Without this, the locator would re-`fopen` the meta-disc on every retry, re-establishing the fd lock.
7. Tier 1 dispatch chdir's out of the meta-disc before exec for belt-and-suspenders fd safety (`recovery/scripts/restore.sh:367`).
8. The tier-1 binary is exec'd with `--meta-disc $META_DISC` so the C-side locator excludes it from its search list and `chdir("/")`s before blocking on user prompts (`recovery/scripts/restore.sh:308`–`recovery/scripts/restore.sh:311`, design at `recovery/docs/MULTI_DISC_DESIGN.txt:476`–`recovery/docs/MULTI_DISC_DESIGN.txt:484`).
9. When the binary needs a pack it cannot find, it prints a prompt that includes a single-drive instruction: "eject the RECOVERY disc first, then insert the disc named above into the SAME drive" (`recovery/docs/MULTI_DISC_DESIGN.txt:485`–`recovery/docs/MULTI_DISC_DESIGN.txt:491`, also user-facing in `recovery/docs/RECOVER.txt:171`–`recovery/docs/RECOVER.txt:180`).
10. The user ejects the meta-disc, inserts the data disc, presses ENTER. The binary re-scans and resumes.

**Expected outcome:**
- The meta-disc has **zero** open fds before the first user prompt — verified by audit at `recovery/docs/MULTI_DISC_DESIGN.txt:493`–`recovery/docs/MULTI_DISC_DESIGN.txt:510`.
- The user can physically eject the meta-disc through the standard tray/eject button and insert a data disc into the same drive.
- All required packs are read; restore exits 0.

**Variant axes that apply:**
- Media type: meta-disc is BD25 or MDISC100; data discs same class.
- OS: *Linux bare-metal initramfs* (typical) or *Linux live USB* (where the USB *replaces* the meta-disc role and no relocation is needed — the USB is already random-access).
- Optical drive count: **1**. This is the defining axis.
- Recovery tier: tier 1; tier 2 also benefits since the entire `bin/` tree is in RAM after relocation.

**Test coverage:**
- Existing:
  - `recovery/tests/test_multidisc.py::case_single_drive_meta_exclusion` — asserts `--meta-disc` causes the locator to exclude that path from search (`recovery/tests/test_multidisc.py:265`).
  - `recovery/tests/test_multidisc.py::case_single_drive_prompt_mentions_eject` — asserts the single-drive prompt text fires (`recovery/tests/test_multidisc.py:306`).
  - `recovery/tests/test_multidisc.py::case_single_drive_script_relocation` — end-to-end relocation: runs `restore.sh` from a read-only directory and verifies it re-execs from a writable RAM dir with no fds on the original (`recovery/tests/test_multidisc.py:612`).
- Gaps:
  - No test physically ejects a disc and re-inserts (impossible in CI). The fd-release audit at `recovery/docs/MULTI_DISC_DESIGN.txt:493`–`recovery/docs/MULTI_DISC_DESIGN.txt:510` is documentary, not asserted by a test that `lsof`s the meta-disc mount.
  - The "best-effort" catalog copy at `recovery/scripts/restore.sh:104`–`recovery/scripts/restore.sh:112` is silent on failure; a corrupt catalog or one too large for `/tmp` is not surfaced to the user.
  - Behavior when no writable scratch dir exists (`recovery/scripts/restore.sh:84`–`recovery/scripts/restore.sh:88`) is to print a warning and continue from the original location — this would leave the disc held. No test covers this degenerate path.

**Source refs:**
- Relocation function: `recovery/scripts/restore.sh:70`–`recovery/scripts/restore.sh:124`
- Relocation trigger logic: `recovery/scripts/restore.sh:129`–`recovery/scripts/restore.sh:160`
- META_DISC propagation: `recovery/scripts/restore.sh:165`, `recovery/scripts/restore.sh:271`–`recovery/scripts/restore.sh:274`, `recovery/scripts/restore.sh:308`–`recovery/scripts/restore.sh:311`, `recovery/scripts/restore.sh:367`
- `lcsas-init` exclusion-path setenv: `recovery/src/lcsas-init/init.c:124`–`recovery/src/lcsas-init/init.c:143`
- Single-drive design + fd audit: `recovery/docs/MULTI_DISC_DESIGN.txt:450`–`recovery/docs/MULTI_DISC_DESIGN.txt:555`
- User-facing instructions: `recovery/docs/RECOVER.txt:161`–`recovery/docs/RECOVER.txt:184`

---

## The `restore.sh` orchestrator inside the live env

**Purpose:** Drive the C-based recovery cascade. `restore.sh` is the only POSIX-shell glue on the bare-minimum path — it relocates to RAM if needed, discovers other mounted volumes, selects a catalog, builds the password-file, picks the architecture, and exec's the highest-priority recovery binary available. Tiers 1–2 are both static binaries; tier 3 (Python) is reachable only if explicitly allowed.

**Prerequisites:**
- A POSIX `sh` (BusyBox ash in the initramfs; any host-OS sh otherwise).
- `RECOVERY_ROOT` containing `repo/keys/`, `repo/index/`, and `bin/<arch>/lcsas-restore` (or `bin/<arch>/rustic-static` for the tier 2 cross-check).
- Password supplied via stdin prompt, `$LCSAS_PASSWORD`, or `$LCSAS_PWFILE` (`recovery/scripts/restore.sh:27`–`recovery/scripts/restore.sh:28`).

**Steps:**
1. Compute `SCRIPT_DIR` via a POSIX-portable `realpath` approximation (`recovery/scripts/restore.sh:34`–`recovery/scripts/restore.sh:38`).
2. Run the single-drive relocation check (see dedicated section above; `recovery/scripts/restore.sh:129`–`recovery/scripts/restore.sh:160`).
3. Resolve `RECOVERY`, `TARGET`, `SNAP` from positional args; auto-detect `RECOVERY` from `$SCRIPT_DIR` when called from inside the recovery tree (`recovery/scripts/restore.sh:167`–`recovery/scripts/restore.sh:203`).
4. Detect architecture via `recovery/scripts/detect_arch.sh` (when present) or `uname -m`; normalize to `x86_64`, `aarch64`, or `riscv64` (`recovery/scripts/restore.sh:207`–`recovery/scripts/restore.sh:219`).
5. Build a temp password file at `/tmp/lcsas-pw.XXXXXX` (chmod 600) from env or stdin; `trap` cleanup on EXIT/INT/TERM (`recovery/scripts/restore.sh:223`–`recovery/scripts/restore.sh:240`).
6. Discover the restic-style repo by looking for `keys/` + `index/` under `$RECOVERY/repo` or `$RECOVERY` itself (`recovery/scripts/restore.sh:244`–`recovery/scripts/restore.sh:255`).
7. Discover additional mounted discs and build `$PACK_SEARCH_ARGS` (`recovery/scripts/restore.sh:266`–`recovery/scripts/restore.sh:304`).
8. Pick the freshest available `catalog.db` and build `$CATALOG_ARG` (`recovery/scripts/restore.sh:320`–`recovery/scripts/restore.sh:358`).
9. Tier 1: if `$RECOVERY/bin/$ARCH/lcsas-restore` is executable, `chdir /` if META_DISC is set, then `exec` it with `--repo`, `--password-file`, `--target`, `--snapshot`, plus the pack-search / catalog / meta-disc args (`recovery/scripts/restore.sh:362`–`recovery/scripts/restore.sh:371`).
10. Tier 2: vendored `rustic-static` cross-check (`recovery/scripts/restore.sh:373`–`recovery/scripts/restore.sh:380`). The bare-minimum path stops here.
11. Tier 3: Python fallback at `recovery/scripts/restore.sh:386`–`recovery/scripts/restore.sh:402`, gated by `LCSAS_ALLOW_PYTHON_TIER` (default 1).
14. If every tier failed, print a help message naming each missing ingredient and exit 1 (`recovery/scripts/restore.sh:442`–`recovery/scripts/restore.sh:455`).

**Expected outcome:**
- The highest-numbered viable tier's binary is exec'd in place; `restore.sh`'s own process is replaced. Exit code is the binary's exit code.
- Common case in a freshly-burned MVP archive: tier 1 fires; everything else is dead code.
- The `[tier N] using ...` log line on stderr tells the user which path was taken.

**Variant axes that apply:**
- Media type: any.
- OS: *Linux bare-metal initramfs* / *Linux live USB* (this section); the same script runs on a working Linux host via `docs/workflows/restore-host-linux.md` and on macOS unchanged.
- Optical drive count: relevant only via the relocation block at the top.
- Recovery tier: this is the dispatcher; tiers 1–3 are all defined here.

**Test coverage:**
- Existing:
  - `recovery/tests/test_bare_path.sh` — proves tiers 1–2 are Python-free both statically (line slice + grep) and dynamically (PATH-shimmed run) (`recovery/tests/test_bare_path.sh:22`–`recovery/tests/test_bare_path.sh:155`).
  - `recovery/tests/test_e2e.py::main` — full tier-1 cascade against a synthetic repo (`recovery/tests/test_e2e.py:277`).
  - `recovery/tests/test_multidisc.py` — covers pack-search auto-discovery, catalog freshest-pick, and single-drive relocation.
- Gaps:
  - No test exercises tier 3 (pure-Python fallback) end-to-end through `restore.sh` — the bare-path test forces it off (`LCSAS_ALLOW_PYTHON_TIER=0`) and `tests/integration/test_pure_python_restore.py` exercises the restorer directly.
  - The arch-detection branch at `recovery/scripts/restore.sh:207`–`recovery/scripts/restore.sh:219` has no negative test for unsupported architectures.
  - The `$LCSAS_PASSWORD` env path at `recovery/scripts/restore.sh:229` is rarely exercised; `LCSAS_PWFILE` is the dominant test path.
  - The freshest-catalog selector breaks if a disc has a clock-skewed mtime; no test pins this corner.

**Source refs:**
- File header / cascade definition: `recovery/scripts/restore.sh:1`–`recovery/scripts/restore.sh:30`
- Relocation: `recovery/scripts/restore.sh:70`–`recovery/scripts/restore.sh:160`
- Arg parsing: `recovery/scripts/restore.sh:167`–`recovery/scripts/restore.sh:203`
- Password handling: `recovery/scripts/restore.sh:223`–`recovery/scripts/restore.sh:240`
- Tier dispatch: `recovery/scripts/restore.sh:360`–`recovery/scripts/restore.sh:402`

---

## Fallback: `restore_auto.sh` (non-interactive multi-repo restore)

**Purpose:** When an LCSAS archive holds **multiple** rustic repos (multi-tenant common case), restore every repo's `latest` snapshot in one shot, with no stdin interaction. Used by scripted disaster-recovery harnesses and by the recovery-toolchain self-tests; can be invoked from inside the bare-metal live env once a password file exists.

**Prerequisites:**
- A pre-prepared password file at `$LCSAS_PWFILE` — there is no stdin prompt path (`recovery/scripts/restore_auto.sh:18`–`recovery/scripts/restore_auto.sh:21`).
- `RECOVERY_ROOT` containing either a `repos/` subdir with one subdir per repo, or a single repo at the root (`recovery/scripts/restore_auto.sh:28`–`recovery/scripts/restore_auto.sh:32`).
- A writable `TARGET_ROOT` — `restore_auto.sh` creates one subdirectory per repo under it.

**Steps:**
1. Validate two positional arguments (`recovery/scripts/restore_auto.sh:13`–`recovery/scripts/restore_auto.sh:17`) and that `LCSAS_PWFILE` points to an existing file (`recovery/scripts/restore_auto.sh:18`–`recovery/scripts/restore_auto.sh:21`).
2. Resolve `repos_root` to `$RECOVERY/repos` if present, else `$RECOVERY` itself (`recovery/scripts/restore_auto.sh:28`–`recovery/scripts/restore_auto.sh:32`).
3. For each subdirectory that contains a `keys/` entry: `mkdir -p $TARGET/<name>` and `sh $RECOVERY/scripts/restore.sh $RECOVERY $TARGET/<name> latest` (`recovery/scripts/restore_auto.sh:34`–`recovery/scripts/restore_auto.sh:45`).
4. Track failures in `$fail`; exit 1 if any repo failed, 0 if all succeeded (`recovery/scripts/restore_auto.sh:25`, `recovery/scripts/restore_auto.sh:47`).

**Expected outcome:**
- Each repo's contents land at `$TARGET/<repo_name>/` with the latest snapshot fully materialized.
- Failures are logged with `!!! restore of <name> FAILED` but do not abort the loop — other repos still attempt their restore.

**Variant axes that apply:**
- Media type: any.
- OS: *Linux bare-metal initramfs*, *Linux live USB*, or any host with a POSIX sh (no host-OS restriction).
- Optical drive count: inherits whatever `restore.sh` does — single-drive relocation triggers on the first repo and the in-RAM relocated copy is reused for all subsequent repos because `LCSAS_RELOCATED` is set in the inherited env.
- Recovery tier: 1 (each invocation dispatches through the full cascade).

**Test coverage:**
- Existing: no direct test of `restore_auto.sh` is wired into the existing fixtures. Indirectly exercised via `test_e2e.py` when a multi-repo layout is built, but the test harness invokes `restore.sh` directly.
- Gaps:
  - No test covers `restore_auto.sh`'s partial-failure semantics (one repo fails, others succeed; exit code 1).
  - No test covers the `$RECOVERY/repos/*/` vs `$RECOVERY/` directory-layout branch.
  - The non-interactive contract (no password prompt) is implicit; a regression that re-introduced a stdin read would not be caught by current tests.

**Source refs:**
- Entry: `recovery/scripts/restore_auto.sh:11`–`recovery/scripts/restore_auto.sh:25`
- Repo discovery: `recovery/scripts/restore_auto.sh:28`–`recovery/scripts/restore_auto.sh:32`
- Per-repo loop: `recovery/scripts/restore_auto.sh:34`–`recovery/scripts/restore_auto.sh:45`

---

## Notes & gaps

Observations from reading the source while assembling this matrix; they are observations, not fixes.

- **`lcsas-init` only probes `/dev/sr*` and `/dev/cdrom`/`/dev/dvd`.** USB sticks, NVMe drives, and HDDs are invisible to `try_discs` (`recovery/src/lcsas-init/init.c:55`–`recovery/src/lcsas-init/init.c:78`). The recovery-USB workflow therefore always drops to the BusyBox shell and the user must mount manually. A `/sys/block` walk would close this gap.
- **The BusyBox shell fallback in `lcsas-init` is the only "manual recovery" route from the initramfs.** Once dropped to that shell there is no on-disc tutorial mounted yet — the inheritor has to know that `mount -o ro /dev/sda1 /mnt && sh /mnt/recovery/scripts/restore.sh /mnt/recovery /tmp/restored latest` is the correct incantation.
- **`restore.sh`'s relocation copies the `bin/` tree but not the holographic `standalone_restorer.py`.** If tier 1 and tier 2 both fail and tier 3 is needed *after* the meta-disc has been ejected, the Python fallback lookup (`recovery/scripts/restore.sh:386`) will fail because the search paths resolve against the in-RAM copy. Either copy `standalone_restorer.py` in `relocate_to_ram` or fail loudly when tier 3 is reached post-relocation.
- **Best-effort catalog copy is silent.** `recovery/scripts/restore.sh:104`–`recovery/scripts/restore.sh:112` redirects errors with `2>/dev/null || true`. If `/tmp` is too small or the catalog is corrupt, the user never knows; downstream prompts will lack volume-label hints.
- **No writable scratch dir = degraded mode without abort.** `relocate_to_ram` returns 1 instead of exiting; `restore.sh` then continues from the original location (`recovery/scripts/restore.sh:84`–`recovery/scripts/restore.sh:88`, `recovery/scripts/restore.sh:156`–`recovery/scripts/restore.sh:159`). The user sees a warning but the next prompt is fatal because the meta-disc cannot be ejected. Hard-failing here might be safer than continuing.
- **The `LCSAS_PASSWORD` env-var path is the weakest leg of the password machinery** — it's written to a temp file on disk (`recovery/scripts/restore.sh:230`) so the password is briefly persisted to the tmpfs. In a bare-metal live env that's only RAM, but on a host-OS path with `/tmp` on disk this is worth flagging.
- **`restore_auto.sh` has no test of its own.** All four exit codes (0, 1, 2 from missing args, 2 from missing pwfile) are unverified by automated test. Documented behavior diverges from tested behavior here.
- **No QEMU smoke test of the actual initramfs.** Every test in `recovery/tests/` runs against `lcsas-restore` directly or against `restore.sh` from a fixture; none boot the kernel + `cpio.gz` end-to-end. The boot stack's correctness is established by inspection of `recovery/docs/BOOT.txt` and `recovery/boot/initramfs/manifest.txt` rather than by execution.
- **FreeBSD alternate boot entry is documented but un-exercised.** `recovery/docs/BOOT.txt:30`–`recovery/docs/BOOT.txt:36` describes the FreeBSD path; no `recovery/boot/freebsd/*` content is referenced from any test.
