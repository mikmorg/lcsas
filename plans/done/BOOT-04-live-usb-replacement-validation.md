# BOOT-04: Validate the live-USB replacement path (Secure Boot, ARM, future drivers)

> **STATUS: RESOLVED** — landed in `17cf25d` (boot+test: gate the live-USB no-OS path -- doc pins + Secure-Boot QEMU e2e [BOOT-04]); guarded by `tests/recovery_hardening/test_live_usb_procedure_docs.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: recovery/docs/UX_CONCERNS.txt:142-153 (ID 007, optical-drive rarity, DEFERRED), docs/CROSS_PLATFORM_META_RFC.md:470-471 (non-x86_64 boot a non-goal). Secure Boot and BIOS-extinction are tracked nowhere.
**Suggested GH issue title:** Gate the live-USB no-OS path: doc pin + Secure-Boot QEMU e2e

## Problem

The audited boot stack is structurally incompatible with the machines an heir will
own in 2035-2050: the GRUB EFI binary is unsigned with no shim (zero mentions of
Secure Boot anywhere in the repo, while consumer machines ship it default-on); the
UEFI path is x86_64-only (`BOOTX64.EFI`, `grub-mkimage -O x86_64-efi`) despite
BOOT.txt claiming aarch64 and riscv64 — Apple Silicon and Snapdragon laptops could
never boot it; the isolinux leg targets legacy BIOS/CSM, which Intel removed from
client platforms in 2020; and a kernel frozen in 2026 lacks drivers for 2040
storage/USB controllers, so the path decays with time. This is the decisive
argument for the DROP verdict executed in BOOT-01: a self-maintained boot medium
fights hardware evolution for 25+ years, while signed mainstream live-Linux images
get Secure Boot, ARM, and current drivers for free by riding contemporary OSes.

BOOT-01 writes the replacement ("boot any current live Linux, then run restore.sh
from the meta disc"). This plan makes the replacement *trustworthy*: an always-on
gate that pins the procedure's load-bearing content into the burned docs, and an
opt-in end-to-end drill that boots a current signed live ISO in QEMU with Secure
Boot enforced, attaches the meta image, and proves a tier-1 restore completes —
so the new no-OS story is tested, unlike the old one (BOOT-08).

## Evidence

(Re-checked 2026-06-10.)

- `grep -rni 'secure.boot\|secureboot\|shim' recovery/ docs/ src/` → no Secure
  Boot hits (only unrelated LD_PRELOAD/python "shim" matches).
- `src/lcsas/meta/bootable.py:249-254` — `_install_efi` searches only
  `BOOTX64.EFI`; `:301` — `grub-mkimage ... "-O", "x86_64-efi"`. No BOOTAA64.EFI
  anywhere.
- `recovery/docs/BOOT.txt:6-7` — "bootable on both legacy BIOS and modern UEFI
  systems, for all three target architectures (x86_64, aarch64, riscv64)" —
  `recovery/bin/` has no riscv64 dir at all (BOOT-01 deletes this claim).
- `docs/CROSS_PLATFORM_META_RFC.md:470-471` — non-goal: "Booting from the
  meta-volume on non-x86_64".
- `recovery/docs/UX_CONCERNS.txt:142-153` — ID 007: optical drives rare by 2030,
  STATUS: DEFERRED, "No code-level mitigation possible."

## Fix design

**A. Always-on doc-pinning gate.**
`tests/recovery_hardening/test_live_usb_procedure_docs.py` (same static
`read_text` style as `test_disc_swap_docs.py`): assert the rewritten
`recovery/docs/BOOT.txt` (BOOT-01's deliverable) and
`docs/workflows/restore-live-usb.md` each contain (1) a concrete live-image
source by name (e.g. "Ubuntu"), (2) the boot-menu key hint (F12/F2), (3) the exact
mount command and (4) the exact `sh /mnt/recovery/scripts/restore.sh` invocation,
and (5) one sentence on *why* a current live image (Secure-Boot signed, current
drivers). Pin strings loosely (case-insensitive substrings) so wording can evolve
without gaming the gate.

**B. Opt-in end-to-end drill** `tests/e2e/test_live_usb_restore.py`, gated on
`LCSAS_LIVE_USB_SMOKE=1` (pattern of `LCSAS_ECC_REPAIR=1`):

1. Fixtures (cached under `/scratch/lcsas-fixtures/`, pinned URL + SHA-256 in
   `tests/e2e/fixtures/live_usb_pins.txt`): current Ubuntu **Server** live ISO
   (server, because its autoinstall hook gives deterministic automation without
   GUI driving); OVMF firmware from the `ovmf` package using the
   Secure-Boot-enabled vars with Microsoft keys enrolled
   (`OVMF_CODE_4M.ms.fd` / `OVMF_VARS_4M.ms.fd` on Ubuntu hosts) — this asserts
   the *signed* shim/GRUB/kernel chain, the property the rewritten docs rely on.
2. Build a tiny meta tree (`lcsas meta build` against a fixture catalog + one
   small rustic repo, as the blind-restore harness does), master it to an ISO
   with xorriso.
3. Boot: `qemu-system-x86_64 -M q35 -m 4096 -drive if=pflash,...OVMF_CODE.ms...
   -drive if=pflash,...vars copy... -cdrom ubuntu.iso -drive
   file=meta.iso,media=cdrom -drive file=target.img,if=virtio -serial stdio
   -display none`, plus a NoCloud seed (`-drive` labeled CIDATA) whose user-data
   `autoinstall.late-commands` mounts `/dev/sr1`, runs
   `sh /mnt/recovery/scripts/restore.sh /target/restored latest` with
   `LCSAS_PASSWORD` injected, and echoes `LCSAS_LIVE_USB_OK <sha256-of-manifest>`
   to the serial console. Inject the `autoinstall console=ttyS0` kernel args via
   QMP `sendkey` at the GRUB menu (a cmdline edit does not break Secure Boot —
   only binaries are signed). Use KVM when `/dev/kvm` exists, TCG otherwise
   (slower; set a 25-minute timeout).
4. Assert the OK marker and the expected restored-content hash on the serial log;
   on timeout, dump the last 100 serial lines into the failure message.

Marker-driven serial assertion keeps the test independent of installer UI churn;
bump the pinned ISO deliberately (the pin file makes drift a reviewed change).

**C. Close the ledger gap.** Update `recovery/docs/UX_CONCERNS.txt` ID 007:
status DEFERRED → MITIGATED, mitigation now includes the live-USB route for
drive-less/no-OS machines (USB BD reader still required to *read* the discs —
keep that text). Add one line for the Secure-Boot rationale so the
2035-hardware argument is tracked somewhere permanent.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_live_usb_procedure_docs.py` — always-on,
  `make test-recovery-hardening`; must fail if BOOT-01's docs regress.
- `tests/e2e/test_live_usb_restore.py` — opt-in `LCSAS_LIVE_USB_SMOKE=1`; wire as
  a **weekly scheduled** job (not per-PR; ~3 GB fixture + minutes of boot) in
  `.github/workflows/` alongside GATE-05's scheduled ECC-repair job; also runnable
  locally (`LCSAS_LIVE_USB_SMOKE=1 pytest tests/e2e/test_live_usb_restore.py -v`).
  Note: this VM is itself a libvirt guest — nested KVM may be unavailable; the
  test must pass under TCG.
- Pin-file integrity: the e2e refuses to run if the downloaded ISO's SHA-256
  mismatches `live_usb_pins.txt` (no silent fixture drift).

## Acceptance criteria

- [ ] Doc gate passes against BOOT-01's rewritten docs and fails against the
      pre-rewrite BOOT.txt.
- [ ] `LCSAS_LIVE_USB_SMOKE=1 pytest tests/e2e/test_live_usb_restore.py -v`
      passes locally: Secure-Boot OVMF boots the stock signed ISO, restore.sh from
      the attached meta image completes tier-1, marker + hash asserted.
- [ ] Weekly workflow exists and has one green scheduled run.
- [ ] UX_CONCERNS ID 007 updated; `grep -rni secureboot recovery/docs/` now hits
      the rationale text.

## Dependencies & related plans

- **BOOT-01** — writes the procedure this plan gates; hard prerequisite.
- **UX-03** — generated START_HERE routing must agree with the gated docs.
- **GATE-02 / GATE-05** — scheduled-CI scaffolding the weekly job rides on.
- **BOOT-08** — records the QEMU *boot-the-meta-disc* gate (revival precondition);
  this plan's QEMU drill validates the *replacement* path instead.

## Effort

2.5 days: 0.5 doc gate + ledger, 1.5 QEMU/autoinstall harness iteration (serial
automation is fiddly; TCG runs are slow), 0.5 CI wiring. Needs ~4 GB scratch for
fixtures; benefits from KVM but must not require it.

---
**Implemented:** 2026-06-11. As planned, with deviations: (1) the QEMU drill
uses autoinstall `early-commands` instead of `late-commands` -- the restore
property is identical but the marker fires without waiting for a full OS
install, which would not fit the plan's own 25-minute TCG budget; (2) GRUB
automation types the casper boot commands at the GRUB shell, synchronised on
the OVMF serial-console mirror, instead of blind menu-editor line navigation
(which proved fragile: a trailing blank editor line landed the appended args
on the initrd line). Validated locally end to end under TCG (no nested KVM):
PASSED in 15:05 -- SecureBoot=1 asserted from guest efivars, tier-1 restore
hash-verified. The weekly workflow is committed but its first scheduled run
is pending (this task does not push).
