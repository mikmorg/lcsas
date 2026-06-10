# BOOT-08: Quarantine tripwire + boot-gate spec — no boot claim without a boot test

**Priority:** P2 · **Severity:** medium · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: recovery/docs/READINESS_CHECKLIST.txt:23-31 ("META DISC BOOT TEST", manual, unchecked); recovery/docs/README.txt:85-87 (runtime validation "deferred"). No automated gate tracked anywhere.
**Suggested GH issue title:** Add boot-path quarantine tripwire; spec QEMU boot gate as revival precondition

## Problem

Nothing in this project has ever booted in any test tier. `tests/unit/
test_bootable.py` fabricates vmlinuz/initramfs/rootfs as 1-2 KB null-byte files
and mocks xorriso; the integration tests check constructor validation only; no
CI workflow mentions QEMU; the only QEMU in the suite is qemu-user for binary
execution (tier-1 aarch64/armv7), not system boot; and the misleadingly named
`test_tier1_meta_disc_live.py` tests disc-locator sentinel behavior. The only
boot validation in the entire project is one unchecked manual checklist line.
That vacuum is why every defect in BOOT-02/03/05/06 shipped invisibly — and why
any future "fix" to the boot path would be equally unverifiable.

With the DROP decision (BOOT-01), the remediation is not to build a boot test
for a deleted path; it is (a) a permanent, always-on **tripwire** that keeps the
quarantine honest — no heir-facing document or supported build flag may
reintroduce a boot promise while no automated boot gate exists — and (b) a
written, concrete **revival precondition**: the QEMU/OVMF boot-smoke gate spec,
recorded where a future implementer must trip over it. The audit framed the gate
as the forcing function: if nobody will fund the gate, that is itself the
evidence the DROP was correct.

## Evidence

(Re-checked 2026-06-10.)

- `tests/unit/test_bootable.py:17-33` — fixtures `write_bytes(b"\x00" * 1024)`
  for vmlinuz/initramfs/rootfs.squashfs.
- `tests/integration/test_recovery_orchestration.py:240-275` —
  constructor/`_validate_inputs` checks only.
- `grep -rn qemu .github/workflows/` → nothing (only `audit-gate.yml`,
  `test.yml` exist); qemu in tests = `test_tier1_aarch64_qemu.py`/
  `test_tier1_armv7_qemu.py` (qemu-user binary exec).
- `tests/recovery_hardening/test_tier1_meta_disc_live.py:1-29` — docstring:
  disc-locator sentinel behavior, not boot.
- `recovery/docs/READINESS_CHECKLIST.txt:23-31` — "[ ] META DISC BOOT TEST",
  manual, unchecked; `recovery/docs/README.txt:85-87` — validation "deferred".
- `recovery/boot/linux/` contains only kernel-config text files — nothing
  bootable has ever existed to test.

## Fix design

**A. Tripwire test** `tests/recovery_hardening/test_boot_path_quarantined.py`
(always-on, static + builder-fixture):

1. Heir-facing docs carry no boot-the-disc instruction: `recovery/docs/
   RECOVER.txt`, `recovery/docs/RECOVER_WINDOWS.txt`, the generated
   `START_HERE.txt` and `README_RESTORE.md` (build via the standard meta-builder
   fixture) must not match `(?i)boot (directly )?(from|the) (the )?(disc|recovery medium)`.
   Allowed exception: BOOT.txt's live-USB text ("boot ... from USB") — anchor
   the regex on disc/medium, not USB.
2. No supported boot surface: `lcsas.cli.main`'s parser exposes no
   `--recovery-boot`/`--bootable` flag; `MetaVolumeBuilder.__init__` has no
   `bootable` parameter (post-BOOT-07; implement as introspection so it fails
   if either is reintroduced).
3. Quarantine record intact: `experimental/boot/README.md` exists, contains the
   "NOT BOOTABLE" banner, the revival-precondition section header, and the
   defect-index entries for BOOT-02/03/05/06 (one substring assertion each —
   this is BOOT-06's requested hook).
4. Re-scope `READINESS_CHECKLIST.txt:23-31`: replace the manual "META DISC BOOT
   TEST" with "verify burned docs advertise no boot path (automated:
   test_boot_path_quarantined.py); boot drill applies only if experimental/boot
   is ever revived." Coordinate with UX-03, which also touches this item.

**B. Revival precondition spec** (written into `experimental/boot/README.md`,
created by BOOT-01 — this plan supplies the section):

- Makefile target `boot-smoke`: build the bootable ISO from real artifacts
  (initramfs via the hard-failing build_initramfs.sh of BOOT-02, a pinned
  kernel listed in UPSTREAM.sha256 per BOOT-07's pinning gate), then
  `qemu-system-x86_64 -M q35 -bios OVMF.fd -cdrom out.iso -serial stdio
  -display none`, asserting the `[lcsas-init] starting` and restore.sh-handoff
  serial markers within 120 s.
- Matrix legs: legacy SeaBIOS boot, and USB attachment (`-device usb-storage`)
  asserting identical markers — covers the isolinux config (BOOT-03) and the
  medium-scan fix (BOOT-06).
- CI: weekly scheduled job (not per-PR) in `.github/workflows/test.yml`-adjacent
  workflow; OVMF + qemu-system-x86_64 are present on Ubuntu runners.
- Rule, stated verbatim: **no heir-facing doc may re-advertise booting until
  this gate exists and is green in CI** — the tripwire's regex is the enforcement.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_boot_path_quarantined.py` — always-on via
  `make test-recovery-hardening`; per-PR CI via GATE-02. Must fail today against
  the pre-BOOT-01 tree (RECOVER.txt:21 trips assertion 1) — implement after
  BOOT-01 and verify by running it against the pre-fix commit.
- The boot-smoke gate itself is deliberately NOT implemented (DROP); its spec is
  content asserted by tripwire item 3, so deleting the spec breaks the suite.

## Acceptance criteria

- [ ] `pytest tests/recovery_hardening/test_boot_path_quarantined.py -v` passes
      on master and fails on the pre-BOOT-01 tree.
- [ ] Reintroducing a `bootable` param on `MetaVolumeBuilder` or a boot-the-disc
      sentence in RECOVER.txt makes the suite fail (verified by mutation).
- [ ] `experimental/boot/README.md` contains the boot-smoke spec with the three
      matrix legs and the no-claim-without-gate rule.
- [ ] READINESS_CHECKLIST boot item re-scoped; no unchecked manual boot drill
      implied for non-bootable builds.

## Dependencies & related plans

- **BOOT-01** — creates the README and reroutes docs; hard prerequisite.
- **BOOT-07** — removes the `bootable` param the introspection asserts against.
- **BOOT-02/03/05/06** — defect-index entries asserted by item 3.
- **UX-03** — shares the READINESS_CHECKLIST re-scope; land once.
- **GATE-02** — puts `tests/recovery_hardening/` in per-PR CI so the tripwire
  actually trips.

## Effort

1 day: 0.5 tripwire test (regexes + parser/builder introspection + mutation
check), 0.5 spec section + checklist re-scope. No special environment.
