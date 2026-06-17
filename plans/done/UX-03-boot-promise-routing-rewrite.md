# UX-03: burned docs promise "boot from the disc" that no build can produce

> **STATUS: RESOLVED** — landed in `602e4f9` (docs+meta: truthful no-OS routing; meta-build flag contract gate [UX-03]); guarded by `tests/unit/test_doc_command_contract.py`.

**Priority:** P0 · **Severity:** high · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: docs/workflows/restore-bare-metal.md:61 (no boot test), recovery/docs/READINESS_CHECKLIST.txt:23-30 (manual drill); the fact that the standard build CANNOT produce a bootable disc and that BOOT.txt's build command is fictional is untracked
**Suggested GH issue title:** Rewrite no-OS route in burned docs; stop advertising unbuildable boot

## Problem

The heir-facing docs offer a third route for "No working computer at all": boot the
meta disc itself (press F12/F2), citing BOOT.txt. That promise is unconditional and
burned onto every meta disc — but no disc the standard build produces is bootable:
`lcsas meta build` never sets `bootable=True` and exposes no flag for it; BOOT.txt's
documented build command uses a `--recovery-boot` flag that does not exist anywhere
in the CLI; `recovery/boot/` contains only configs and build scripts (no vmlinuz, no
initramfs, no isolinux/EFI loaders); and the alternate Alpine path raises unless
given pre-built artifacts that no Makefile target produces.

An heir whose only machine is dead follows the on-disc instruction, gets a
non-booting disc, and has no explanation and no alternative route — the exact
scenario the route exists for. The fix here is the **routing rewrite**: the burned
text must describe a path that actually works today (use another computer / a
current live-Linux USB), and must only advertise booting when the builder actually
made a bootable volume. Whether the boot stack is ever revived or demoted to
`experimental/` is BOOT-01's decision, not this plan's.

## Evidence

(Re-checked 2026-06-10.)

- `src/lcsas/meta/builder.py:2451-2454` — no-config START_HERE: `>>> No working
  computer at all <<<  Boot directly from the disc. ... (See recovery/docs/BOOT.txt
  for details.)` — written unconditionally.
- `src/lcsas/cli/main.py:387-398` — `meta build` argparse has only `--output` and
  `--project-root`; `:1951-1956` — `MetaVolumeBuilder(...)` constructed without
  `bootable`/`alpine_dir`. `grep -rn 'recovery-boot' src/lcsas/cli/` → no hits.
- `recovery/docs/BOOT.txt:66-67` — "Produce a bootable ISO:
  `lcsas meta build --output meta/ --recovery-boot`" — fictional flag.
- `recovery/boot/linux/` = `cmdline.txt` + three `kernel_config.*.txt`;
  `recovery/boot/freebsd/` = `kernel_config.txt` + `loader.conf` — configs only, zero
  binary artifacts.
- `src/lcsas/meta/builder.py:1742-1753` — `_install_live_boot` raises `ValueError`
  without `alpine_dir` and `FileNotFoundError` without pre-built
  vmlinuz/initramfs/rootfs.squashfs.
- `docs/RECOVERY_GUIDE.md:35` — "No working OS | ... restore-bare-metal.md (boot the
  META disc directly)" and `:218` — "No working OS?  See restore-bare-metal.md (boot
  META)."

## Fix design

**A. Conditional, truthful no-OS block in generated START_HERE.**
In `src/lcsas/meta/builder.py._write_start_here` (and the merged META variant from
UX-05), replace the boot block; the builder knows `self._bootable`:

- `bootable=False` (every current build):
  ```
  >>> No working computer at all <<<
       These discs do NOT require a special computer.  Use any other
       computer — a friend's, a library's, or a cheap second-hand
       laptop.  Windows, macOS and Linux all work (pick your section
       above).
       If a computer has no operating system at all, ask a helper to
       make a "live Linux USB stick" (free; search for
       "create Ubuntu live USB"), start the computer from it, then
       follow the macOS/Linux steps above.
  ```
- `bootable=True` (only reachable if BOOT plans ever ship real artifacts): keep the
  boot text, and only after `_install_live_boot` has verified the artifacts exist.

**B. Same routing fix in static heir docs.**
- `docs/RECOVERY_GUIDE.md:35` and `:218` — route "No working OS" to the live-USB
  walkthrough (BOOT-01 deliverable `docs/workflows/restore-live-usb.md`; until it
  exists, route to the macOS/Linux section of RECOVER.txt with the live-USB hint).
- `recovery/docs/BOOT.txt:66-67` — remove the phantom `--recovery-boot` command;
  state plainly that the standard build is **not** bootable and that the boot stack
  is experimental/aspirational (final framing per BOOT-01's keep/demote decision).
- `docs/workflows/restore-bare-metal.md` — add a banner: this workflow describes a
  path the current build does not produce; see restore-live-usb.md.

**C. Contract-gate extension (rides UX-02's skeleton).**
Add to `tests/unit/test_doc_command_contract.py`: every `lcsas meta build --<flag>`
mention in `recovery/docs/` and `docs/` must exist in the argparse definition
(introspect the parser from `lcsas.cli.main`). This kills the `--recovery-boot`
class permanently.

No catalog/schema impact. Already-burned discs carry the false promise forever —
mitigate by ensuring RECOVER.txt (which heirs reach next) carries the corrected
no-OS routing, and note the erratum in the next meta-disc re-burn guidance
(READINESS_CHECKLIST).

## Tests & gates

- `tests/unit/test_meta_builder.py::test_start_here_boot_claim_matches_bootability` —
  build with `bootable=False` (the fixture default) and assert START_HERE.txt does
  NOT contain "Boot directly from the disc" and DOES contain the live-USB guidance;
  existing fixture at `tests/unit/test_meta_builder.py:347-361` only checks section
  presence today.
- `tests/unit/test_doc_command_contract.py::test_meta_build_flags_in_docs_exist` —
  as in Fix C; always-on via `make test-unit` / CI test.yml. Must fail on the
  pre-fix BOOT.txt.
- Manual: `recovery/docs/READINESS_CHECKLIST.txt:23-30` "META DISC BOOT TEST" —
  re-scope to "only applicable when building with a bootable path; otherwise verify
  START_HERE does not advertise booting" so the checklist stops implying a drill the
  build cannot satisfy.
- Opt-in QEMU boot smoke: owned by the BOOT plans (only meaningful if boot is revived).

## Acceptance criteria

- [ ] A fresh `lcsas meta build` output's START_HERE.txt contains no "boot directly
      from the disc" claim and offers the borrow-a-computer + live-USB route.
- [ ] `grep -rn 'recovery-boot' recovery/ docs/ src/` returns no doc references to the
      phantom flag.
- [ ] RECOVERY_GUIDE.md no-OS rows route to a path that exists today.
- [ ] `pytest tests/unit/test_meta_builder.py tests/unit/test_doc_command_contract.py -v` passes.

## Dependencies & related plans

- **BOOT-01** ("Bootable meta-volume is unreachable scaffolding...") — owns the
  structural decision (demote `recovery/boot/`+`meta/live` to experimental vs. revive)
  and the live-USB walkthrough doc; this plan must not duplicate it. Land UX-03's
  text/routing fixes first — they are correct under either BOOT-01 outcome.
- **UX-02** — provides the contract-test skeleton this plan extends.
- **UX-05** — the META-specific START_HERE generator; coordinate so the no-OS block
  is written once.

## Effort

1.5 days: 0.5 generated-text + static-doc rewrites, 0.5 contract/unit tests,
0.5 coordination with UX-05 generator merge. No special environment.

---
**Implemented:** 2026-06-11. Mostly as planned; BOOT-01 (landed first) had already
fixed BOOT.txt, RECOVER.txt, the START_HERE boot text, and shipped restore-live-usb.md,
so this plan delivered the remainder: conditional bootable-aware START_HERE no-OS block
(install/verify live-boot before writing START_HERE; richer borrow-a-computer + live-USB
text), RECOVERY_GUIDE.md no-OS rows -> restore-live-usb.md, restore-bare-metal.md
status banner, READINESS_CHECKLIST blind-drill/quick-ref fixes + pre-2026-06 burned-disc
erratum, and the `lcsas meta build` flag contract gate (which caught and fixed two real
phantom usages: `--config`/`--db` after the subcommand in meta-volume.md:579).
