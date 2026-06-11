# BOOT-06: lcsas-init probes only optical device nodes — USB boot dead-ends at a shell

**Priority:** P2 · **Severity:** medium · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Record lcsas-init optical-only scan limitation; spec sentinel-based fix

## Problem

The bootable-ISO builder applies `isohybrid --uefi` specifically so the image can
be written to and booted from a USB stick — the realistic medium for 2035+
machines without optical drives. But `lcsas-init`'s medium search probes only
`/dev/sr0-3`, `/dev/cdrom`, `/dev/dvd`. Booted from USB, the medium is `/dev/sdX`,
never probed: init logs "WARNING: no optical disc found; dropping to shell" and a
non-technical heir lands at a bare BusyBox prompt (or, given BOOT-02's zero-byte
busybox, the FATAL infinite-sleep loop). The Alpine stack's init *does* scan
`/dev/sd[a-z]` but keys off a different sentinel (`rootfs.squashfs`) — one more
instance of the two-stack divergence (BOOT-07).

The boot path is dropped (BOOT-01), so no supported journey reaches this code.
The remaining work is to make the quarantined material honest — record the
limitation where a future reviver will see it — and to spec the correct fix so a
revival doesn't re-derive it from scratch. `lcsas-init` itself stays where it is
(`recovery/src/lcsas-init/`, built into `recovery/bin/<arch>/`, pinned in
MANIFEST.sha256): it is inert without a kernel, and moving it would churn the
recovery Makefile and binary manifest for no heir-visible gain.

## Evidence

(Re-checked 2026-06-10.)

- `recovery/src/lcsas-init/init.c:57-60` — candidates array is exactly
  `/dev/sr0..sr3, /dev/cdrom, /dev/dvd`.
- `recovery/src/lcsas-init/init.c:115-121` — scan miss → "no optical disc found;
  dropping to shell" → busybox shell exec.
- `src/lcsas/meta/bootable.py:484-511` — `_make_hybrid` applies isohybrid
  ("bootable from USB too"), adding `--uefi` when `boot/efiboot.img` exists.
- `src/lcsas/meta/live/init:40-41` — Alpine init scans
  `/dev/sd[a-z] /dev/sd[a-z][0-9]` with sentinel `boot/rootfs.squashfs`.
- `recovery/tests/test_disc_locator.c` covers lcsas-restore's disc locator, not
  init's device scan — no existing coverage.

## Fix design

1. **Record (this plan's deliverable).** Add to `experimental/boot/README.md`'s
   defect index (BOOT-01): "lcsas-init probes optical nodes only; a USB-booted
   image cannot find itself." Add a header comment in
   `recovery/src/lcsas-init/init.c` above `try_discs()` stating the limitation
   and pointing at the README entry.
2. **Spec the KEEP-variant fix (recorded, not implemented).** In the same README:
   extend `try_discs()` to probe `/dev/sd[a-z]` (+ partitions 1-4), `/dev/vd[a-z]`,
   `/dev/nvme[0-3]n1(p1-4)`, and identify the recovery medium by sentinel —
   `access("/mnt/recovery/scripts/restore.sh")` after each successful read-only
   mount, unmounting non-matches — rather than by device class. Sentinel
   selection mirrors `meta_disc_is_live` in lcsas-restore's disc_locator. Required
   tests for revival: `recovery/tests/test_init_medium_scan.c` (candidate/sentinel
   logic against a fake /dev tree, style of `test_disc_locator.c`) and a USB-attach
   leg in the QEMU boot smoke (BOOT-08 spec): same image attached as
   `usb-storage` instead of `-cdrom`, same serial handoff marker.

No catalog/schema impact; no behavior change to shipped binaries.

## Tests & gates

- Covered by BOOT-08's `tests/recovery_hardening/test_boot_path_quarantined.py`:
  one assertion that `experimental/boot/README.md` lists the optical-only-scan
  defect (keeps the record from silently vanishing).
- No new runtime test now — the code is unreachable by design; the C unit test
  and QEMU USB leg above are gated behind revival (BOOT-08 preconditions).

## Acceptance criteria

- [ ] `experimental/boot/README.md` documents the optical-only limitation and the
      sentinel-scan fix spec with its two required tests.
- [ ] `init.c` carries the limitation comment above `try_discs()`.
- [ ] BOOT-08's quarantine test asserts the README entry exists.

## Dependencies & related plans

- **BOOT-01** — creates `experimental/boot/README.md`; land first.
- **BOOT-08** — hosts the tripwire assertion and the revival-gate spec this plan
  feeds. **BOOT-07** — the Alpine init divergence this finding underscores.

## Effort

0.5 day (docs + comment + one test assertion). No special environment.

---
**Implemented:** 2026-06-11. As planned, with two notes: BOOT-08 has not landed, so `tests/recovery_hardening/test_boot_path_quarantined.py` was created here carrying only the BOOT-06 tripwire (BOOT-08 will extend it); `recovery/bin/*/lcsas-init` is gitignored, so the comment-only `init.c` change required no binary regeneration — only the `init.c` line in `recovery/MANIFEST.sha256` was re-pinned.
