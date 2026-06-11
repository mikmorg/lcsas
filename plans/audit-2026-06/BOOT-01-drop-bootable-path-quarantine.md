# BOOT-01: Drop the bootable meta-volume; quarantine the scaffolding; reroute the no-OS journey

**Priority:** P0 · **Severity:** high · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: recovery/docs/README.txt:85-87 (runtime validation "deferred"), recovery/docs/READINESS_CHECKLIST.txt:23-31 (manual boot test, unchecked). The phantom `--recovery-boot` flag and the RECOVER.txt/START_HERE routing into a non-bootable disc are untracked.
**Suggested GH issue title:** Drop bootable meta-volume path; quarantine boot scaffolding to experimental/

## Problem

Every heir-facing document routes the "no working computer" scenario to booting the
meta disc itself: RECOVER.txt's decision flow says "NO -> Boot the recovery medium
directly. See BOOT.txt", and the START_HERE.txt burned onto every meta disc says
"Boot directly from the disc. Most computers boot from optical media if you press
F12 or F2 at power-on." But **no disc any build has ever produced is bootable.**
`lcsas meta build` accepts only `--output` and `--project-root`; the
`MetaVolumeBuilder(bootable=...)` parameter defaults to `False` and is never set by
any caller; BOOT.txt documents a `--recovery-boot` CLI flag that exists nowhere in
the argparse tree; and no kernel, initramfs, or BusyBox artifact exists anywhere in
the repository (`recovery/boot/linux/` contains only `kernel_config.*.txt` text
files). An heir with a dead computer follows the on-disc instruction, gets
"no bootable device", and has no hint the path is fictional — the exact scenario
the route exists to save.

The audit's verdict (and this plan's mandate) is **DROP**: the boot path is
unreachable scaffolding, and even fully built it would fight 25 years of hardware
evolution (Secure Boot, ARM, driver decay — see BOOT-04) that any current signed
live-Linux image solves for free. This plan executes the drop: quarantine
`recovery/boot/` to `experimental/`, rewrite BOOT.txt as a live-USB procedure,
fix the RECOVER.txt routing, and deliver the live-USB walkthrough doc that UX-03
(the doc-side counterpart, which owns the generated START_HERE text) routes to.
The KEEP alternative — build the kernel/initramfs/BusyBox stack and gate it with a
QEMU/OVMF boot smoke — is recorded in BOOT-08 as the only acceptable revival path;
it is not pursued because it converts a one-time fix into a permanent
OS-vendor-sized maintenance liability.

## Evidence

(All re-checked 2026-06-10 against master.)

- `src/lcsas/cli/main.py:387-399` — `meta build` argparse defines only `--output`
  and `--project-root`. `:1951-1956` — `MetaVolumeBuilder(output_dir=..., project_root=...,
  config=..., catalog_db_path=...)`; `bootable`/`alpine_dir` never passed.
- `src/lcsas/meta/builder.py:1647` — `bootable: bool = False`; `:1726` —
  `if self._bootable:` is the only call site of `_install_live_boot` → dead code.
- `recovery/docs/BOOT.txt:67` — "`lcsas meta build --output meta/ --recovery-boot`"
  — flag does not exist (`grep -rn 'recovery-boot' src/lcsas/cli/` → no hits).
- `recovery/docs/RECOVER.txt:21` — "`NO  -> Boot the recovery medium directly.  See BOOT.txt.`"
- `src/lcsas/meta/builder.py:2451-2454` — START_HERE heredoc: ">>> No working
  computer at all <<<  Boot directly from the disc. ... (See recovery/docs/BOOT.txt
  for details.)" — burned onto every disc (text fix owned by UX-03).
- `recovery/boot/linux/` = `cmdline.txt` + 3 `kernel_config.*.txt`;
  `recovery/boot/freebsd/` = `kernel_config.txt` + `loader.conf`. No kernel,
  loader, or BusyBox binary anywhere (`recovery/bin/<arch>/` ships only
  lcsas-restore/iso9660/init/keyshare).
- `src/lcsas/meta/builder.py:1974-1981` — `_bundle_recovery_toolchain_artifacts`
  `shutil.copytree`s the entire `recovery/` tree onto every meta-volume, so the
  dead `boot/` configs and the fictional BOOT.txt ship on-disc today.
- `recovery/MANIFEST.sha256:1-10` — pins the `boot/` config files; moving them
  requires `make -C recovery manifest` regeneration.

## Fix design

**A. Decision record.** Add `experimental/boot/README.md` opening with a decision
block: status NOT BOOTABLE / NEVER BUILT; dropped 2026-06 per deep audit; revival
precondition is the automated QEMU/OVMF boot gate (spec in BOOT-08); known defects
indexed (BOOT-02 zero-byte initramfs, BOOT-03 config path mismatches, BOOT-05
tmpfs target, BOOT-06 optical-only device scan).

**B. Quarantine `recovery/boot/` → `experimental/boot/`.**
- `git mv recovery/boot experimental/boot`. Because the meta builder copies only
  `recovery/` (builder.py:1974), the move *automatically* stops shipping the dead
  configs and scripts on burned discs — no builder change needed for this step.
- Regenerate `recovery/MANIFEST.sha256` (`make -C recovery manifest`) so the moved
  files leave the integrity manifest; commit the regenerated file in the same PR.
- Fix dangling references: `src/lcsas/meta/bootable.py:131-134` error text
  ("Build with: ... bash boot/initramfs/build_initramfs.sh ...") and
  `recovery/docs/README.txt:44-45` directory map (the Python-side `bootable.py`/
  `meta/live` disposition is BOOT-07's; only fix the path strings here).
- Leave `recovery/src/lcsas-init/` and the built `lcsas-init` binaries in place
  this PR (inert without a kernel; moving them churns recovery/Makefile `all`
  targets and MANIFEST binary rows — see BOOT-06 for the recorded limitation).

**C. Rewrite `recovery/docs/BOOT.txt` as the live-USB procedure.** Keep the
filename (referenced from README.txt:26, READINESS_CHECKLIST.txt:186). New content:
1. Banner: "The LCSAS discs are NOT bootable. If no computer with a working
   operating system is available, follow this page."
2. Option 1 — use any other computer (friend, library, second-hand laptop);
   point back to START_HERE's per-OS sections.
3. Option 2 — live-Linux USB: on any working machine, download a current
   mainstream Linux live image (name Ubuntu Desktop LTS as the concrete example
   and say "any current major Linux works"), write it to a USB stick (name the
   distro's official instructions, e.g. "search: create Ubuntu live USB"), boot
   the dead machine from USB (F12/F2/Del boot menu), choose "Try without
   installing", then:
   ```
   mount -o ro /dev/sr0 /mnt        (or click the disc in the file manager)
   sh /mnt/recovery/scripts/restore.sh /home/ubuntu/restored latest
   ```
   Explicitly state why this works decades out: current live images are
   Secure-Boot signed and carry current hardware drivers (cross-ref BOOT-04).
4. Remove entirely: the x86_64/aarch64/riscv64 bootable claims (old BOOT.txt:6-7),
   the four-entry boot menu description, the `--recovery-boot` command (old :67),
   and the "REBUILDING THE BOOT STACK" section (point to `experimental/boot/`).
- Also create `docs/workflows/restore-live-usb.md` with the same procedure in
  repo-docs form — UX-03 explicitly routes `docs/RECOVERY_GUIDE.md` and
  `restore-bare-metal.md` to this file and names BOOT-01 as its owner.

**D. Fix the RECOVER.txt routing.** `recovery/docs/RECOVER.txt:21`:
```
  Q: Do you have a working OS?
     NO  -> Use any other computer, or boot a current live-Linux
            USB stick on this one.  See BOOT.txt for the steps.
     YES -> Continue.
```

**E. Update prose status claims.** `recovery/docs/README.txt:26` (describe BOOT.txt
as "no-OS recovery procedure (live-USB)"), `:85-87` (replace "deferred to the build
host" with "boot stack dropped; see experimental/boot/"). The "Phase 2 COMPLETE /
BusyBox" claim correction is BOOT-02's; READINESS_CHECKLIST re-scope is shared
with UX-03 — land whichever PR is first, the other rebases.

No catalog/schema impact. Already-burned discs carry the old routing forever; the
mitigation is that the corrected RECOVER.txt/BOOT.txt ship on every future disc
and the next re-burn guidance (READINESS_CHECKLIST) notes the erratum.

## Tests & gates

- `tests/recovery_hardening/test_no_boot_deadend_routing.py` (new, always-on,
  static read_text style of `test_disc_swap_docs.py`):
  - `recovery/docs/RECOVER.txt` no-OS branch does NOT contain "Boot the recovery
    medium" and DOES contain "live" + "USB".
  - `recovery/docs/BOOT.txt` contains the NOT-bootable banner and the exact
    `sh /mnt/recovery/scripts/restore.sh` command; contains no `--recovery-boot`.
  - `src/lcsas/meta/builder.py` source contains no "Boot directly from the disc"
    heredoc text (belt-and-braces for UX-03; drop this assertion if UX-03's
    builder test already covers it).
- `tests/recovery_hardening/test_boot_docs_reality.py` (new, always-on): extract
  every `lcsas ...` invocation from `recovery/docs/*.txt` and assert each
  subcommand/flag exists in the argparse tree built by `lcsas.cli.main`. This is
  the recovery-docs leg of UX-02's docs-vs-reality contract gate — implement on
  UX-02's skeleton if it lands first; do not build a second extractor.
- Move-integrity check: full `make test-unit && make test-recovery-hardening`
  green after the `git mv` (catches any code path still importing/reading
  `recovery/boot/`); `make -C recovery test` green (Makefile untouched by the move
  except path strings).
- Runs via `make test-recovery-hardening`; per-PR CI coverage of that suite is
  GATE-02's deliverable.

## Acceptance criteria

- [ ] `recovery/boot/` no longer exists; `experimental/boot/README.md` carries the
      decision record and defect index.
- [ ] A fresh `lcsas meta build` output contains no `recovery/boot/` directory and
      its `recovery/docs/BOOT.txt` is the live-USB procedure.
- [ ] `grep -rn 'recovery-boot' recovery/ docs/ src/` → no doc hits (the
      `recovery_boot_dir` code identifier is BOOT-07's to remove).
- [ ] `grep -n 'Boot the recovery medium' recovery/docs/RECOVER.txt` → no hits.
- [ ] `sha256sum -c recovery/MANIFEST.sha256` passes from `recovery/`.
- [ ] `docs/workflows/restore-live-usb.md` exists with the mount + restore.sh
      commands (pinned by BOOT-04's doc gate).
- [ ] `pytest tests/recovery_hardening/test_no_boot_deadend_routing.py
      tests/recovery_hardening/test_boot_docs_reality.py -v` passes, and both fail
      when run against the pre-fix tree.

## Dependencies & related plans

- **UX-03** (boot-promise routing rewrite) — owns the generated START_HERE text and
  static `docs/` routing; expects this plan to deliver `restore-live-usb.md`. Land
  UX-03 first or together; its text fixes are correct under this plan's outcome.
- **UX-02** (docs-vs-reality contract gate) — provides the command-extractor
  skeleton reused by `test_boot_docs_reality.py`.
- **BOOT-07** — Python-side removal (meta/live, bootable.py, builder params);
  do after this move so both quarantines land in one release.
- **BOOT-02/03/06** — defects in the quarantined material; their fixes apply under
  `experimental/boot/`. **BOOT-04** — validates the replacement live-USB path.
  **BOOT-08** — quarantine tripwire + revival precondition.
- **GATE-02** — wires `tests/recovery_hardening/` into per-PR CI so these gates run.

## Effort

2 days: 1.0 quarantine move + manifest regen + reference sweep, 0.5 BOOT.txt /
RECOVER.txt / restore-live-usb.md rewrite, 0.5 tests. No special environment.

---
**Implemented:** 2026-06-11. As planned, plus: UX-03 had not landed, so the builder.py minimal START_HERE heredoc was rerouted here (required by the belt-and-braces test); the new docs-reality gate also caught fictional `restore exec --snapshot/--target` flags in READINESS_CHECKLIST.txt (fixed to positional form) and the META DISC BOOT TEST entry was rescoped to a live-USB drill (shared-with-UX-03 item, landed here first). Manifests regenerated from a pristine index export so local gitignored fuzz-corpus files are not pinned.
