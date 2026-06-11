# BOOT-07: Remove the Alpine live stack; one (or zero) boot implementations, all artifacts pinned

**Priority:** P2 · **Severity:** medium · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: docs/CROSS_PLATFORM_META_RFC.md:470-471 (Alpine arch story out of scope). The duplication, unpinned network-fetched boot artifacts, and untested wizard are untracked.
**Suggested GH issue title:** Delete Alpine live stack and bootable builder params; gate unpinned artifacts

## Problem

The repo maintains two divergent, half-built boot implementations: the Alpine
live stack (`src/lcsas/meta/live/`: `build_rootfs.sh`, `init`, grub/isolinux
configs, a 33 KB `restore_wizard.py` TUI) and the C89 recovery stack
(`recovery/boot/` + lcsas-init). The Alpine path fetches packages from Alpine 3.21
repos over the network at build time (`apk update && apk add ... linux-lts`),
hardcodes `ARCH=x86_64`, and none of its outputs (vmlinuz/initramfs/
rootfs.squashfs) are pinned in UPSTREAM.sha256 or MANIFEST.sha256 — violating the
project's own doctrine that every shipped runtime artifact is pinned. The
heir-facing wizard has only import/attribute smoke tests. The docs contradict
each other about which stack is real (BOOT.txt describes the C89 stack as "the"
boot path; the RFC calls Alpine "a separate effort"). Maintaining both doubles
the staleness surface while neither has ever booted.

Per the audit verdict (DROP, executed by BOOT-01 for the C89 side), this plan
removes the Python/Alpine side entirely: delete `src/lcsas/meta/live/`, remove
the `bootable`/`alpine_dir` machinery from `MetaVolumeBuilder`, and quarantine
`src/lcsas/meta/bootable.py` next to the C89 material in `experimental/`. It also
adds the generalizable guard the finding exposed: nothing executable may reach a
meta-volume staging tree without appearing in a pinning manifest, and meta build
scripts must not fetch from the network.

## Evidence

(Re-checked 2026-06-10.)

- `src/lcsas/meta/live/build_rootfs.sh:30-46` — `ALPINE_VERSION="3.21"`,
  `ARCH="x86_64"`, `apk update`, `apk add --root ... alpine-base linux-lts ...`
  (network, unpinned).
- `recovery/UPSTREAM.sha256:1-20` — header lists only `rustic/` and `python/`
  categories; `grep -i 'alpine\|vmlinuz\|busybox' recovery/*.sha256` matches only
  `kernel_config*.txt` doc rows in MANIFEST.sha256.
- `tests/unit/test_bootable.py:355-378` — wizard tests are import/`hasattr`/
  init-defaults/dialog-missing smoke only.
- `src/lcsas/meta/builder.py:1647` (`bootable: bool = False`), `:1726`
  (`if self._bootable:`), `:1736-1777` (`_install_live_boot` — the only Alpine
  wiring; raises without `alpine_dir` artifacts; copies `restore_wizard.py`).
  No caller anywhere passes `bootable=True` (CLI constructs the builder at
  `src/lcsas/cli/main.py:1951-1956` without it).
- `src/lcsas/meta/bootable.py:81-87` — `BootableISOBuilder` reachable only by
  direct construction (`alpine_dir` XOR `recovery_boot_dir`).
- `docs/CROSS_PLATFORM_META_RFC.md:470-471` — Alpine boot declared a separate
  effort/non-goal.

## Fix design

**A. Delete the Alpine stack.** Remove `src/lcsas/meta/live/` entirely (wizard,
init, build_rootfs.sh, grub.cfg, isolinux.cfg). The wizard is unreferenced
outside `_install_live_boot` and its smoke tests.

**B. Strip `MetaVolumeBuilder`.** Remove `bootable`/`alpine_dir` params
(builder.py:1647-1648, 1674-1675), the `:1726` branch, and `_install_live_boot`
(:1736-1777). Update the class docstring layout block. CLI needs no change
(never passed them).

**C. Quarantine `BootableISOBuilder`.** `git mv src/lcsas/meta/bootable.py
experimental/boot/bootable.py`, deleting its `alpine_dir` mode (the
`recovery_boot_dir` mode is the only one with a counterpart in
`experimental/boot/`); it stops being part of the installed `lcsas` package
(it's under `experimental/`, outside `[tool.setuptools.packages.find]`'s `src/`
root). BOOT-03's recovery-mode config fixes then apply to this file. Decision
note: quarantine rather than delete, because BOOT-08's revival precondition
needs a concrete starting artifact; deleting is acceptable if the team prefers —
say so in the PR and BOOT-03/08 degrade gracefully (their builder-dependent
tests are skip-if-absent).

**D. Update tests.** Delete `TestRestoreWizard` and the Alpine fixtures/tests in
`tests/unit/test_bootable.py` (fixtures at :17-33 write fake null-byte
artifacts); delete the `BootableISOBuilder` constructor-validation tests there
and in `tests/integration/test_recovery_orchestration.py:240-275`, or move the
few recovery-mode ones alongside the experimental module (BOOT-03's call).
`tests/recovery_hardening/test_tier1_meta_disc_live.py` is disc-locator only —
unaffected.

**E. New guards (the lasting fix).**
- `tests/recovery_hardening/test_no_unpinned_boot_artifacts.py` — build a meta
  tree via the standard fixture path and assert every regular file in the output
  that is executable, or named `vmlinuz*`/`initramfs*`/`*.squashfs`/`*.efi`,
  appears in the bundled `recovery/MANIFEST.sha256` or `recovery/UPSTREAM.sha256`
  (extends the inventory approach of `test_meta_bundling_completeness.py`; note
  `_regenerate_recovery_manifest` at builder.py:2012-2020 already rebuilds the
  manifest at bundle time — the test asserts the *coverage* of that manifest).
- `test_no_network_fetch_in_meta_scripts` (same file) — no script under
  `src/lcsas/meta/` contains `apk |curl |wget |git clone` tokens (allowlist
  comment marker for pinned-fetch exceptions, mirroring fetch_upstream.sh which
  lives under `recovery/scripts/` and is manifest-pinned).

No catalog/schema impact. Old burned discs may carry a `restore_wizard.py` at the
volume root; it was never referenced by any on-disc doc or script, so no
restore-side tolerance code is needed.

## Tests & gates

- `make test-unit` + `make test-recovery-hardening` green post-deletion (catches
  dangling imports; `mypy` strict and `ruff` per `make typecheck`/`make lint`).
- The two new guard tests above: always-on in `tests/recovery_hardening/`,
  run by `make test-recovery-hardening`; per-PR CI via GATE-02.
- `test_no_unpinned_boot_artifacts.py` must fail if a future change stages an
  executable into the meta tree without a manifest row (inject one in the test
  to prove the detector).

## Acceptance criteria

- [ ] `src/lcsas/meta/live/` does not exist; `grep -rn "meta.live\|meta/live" src/ tests/` → no hits.
- [ ] `MetaVolumeBuilder` has no `bootable`/`alpine_dir` parameters;
      `grep -rn "_install_live_boot\|alpine_dir" src/` → no hits.
- [ ] `BootableISOBuilder` is absent from the installed package
      (`python -c "import lcsas.meta.bootable"` fails in a fresh `make dev` env).
- [ ] Both new guard tests pass, and the unpinned-artifact test fails on a
      planted unmanifested executable.
- [ ] `make lint typecheck test-unit test-recovery-hardening` all green.

## Dependencies & related plans

- **BOOT-01** — quarantines the C89 side and creates `experimental/boot/`; land
  first so Fix C has a destination.
- **BOOT-03** — edits the quarantined builder's recovery mode after this move.
- **BOOT-02** — its `_validate_inputs` cpio check follows the class to
  `experimental/boot/bootable.py`.
- **RST-05** (meta-volume completeness gate) — the manifest-coverage test should
  share fixtures with it; coordinate to avoid two meta-build fixtures.

## Effort

1.5 days: 0.5 deletion + builder strip + test cleanup, 0.5 quarantine move +
import/packaging checks, 0.5 the two guard tests. No special environment.

---
**Implemented:** 2026-06-11. As planned, with two small calls: the BOOT-03 builder tests stay in tests/unit/test_boot_config_paths.py but load the quarantined module from its file path (skip-if-absent), and the generic xorriso create_bootable_iso tests moved into tests/unit/test_xorriso.py rather than being deleted.
