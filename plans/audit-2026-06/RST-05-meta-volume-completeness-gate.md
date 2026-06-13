# RST-05: No meta-volume completeness gate — incomplete rescue discs pass every check

**Priority:** P1 · **Severity:** high · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: partially — tests/recovery_hardening/test_meta_bundling_completeness.py (tier-1 source-tree only, local-only); READINESS_CHECKLIST.txt boot test is content-blind
**Suggested GH issue title:** Add required-contents contract to meta build and meta verify

## Problem

The meta-volume is the rescue disc: per-target tier-1 binaries, upstream rustic-static, bundled
CPython trees, restore scripts. `MetaVolumeBuilder` treats every per-target artifact as
optional — missing upstream cache → silent return; each absent per-arch `lcsas-restore` →
`continue`; the silent skip is even pinned as intended behavior by a unit test. Then
`_regenerate_recovery_manifest` rebuilds `MANIFEST.sha256`'s `bin/` rows FROM whatever happened
to get bundled, so `lcsas meta verify` (and a future heir's `sha256sum -c`) validates an
incomplete bundle as PASS — it pins what WAS shipped, not what SHOULD ship. Root-level restore
artifacts (`standalone_restorer.py`, `keyshare_combine.py`, `tools/`, `restore.bat`) are in no
manifest at all.

Net effect: an operator can burn a "rescue" disc missing the Windows/macOS/aarch64 binaries —
or tier-1 entirely — with zero failing gate, and the heir on that platform discovers it decades
later. The only completeness-adjacent check today is the manual "META DISC BOOT TEST" checklist
item, which tests boot, not contents; the existing
`test_meta_bundling_completeness.py` gates only tier-1 binaries, only in the **source tree**
(not the built output), and only locally.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/meta/builder.py:1962-1965` — docstring codifies the policy: "Missing per-arch
  binaries are silently skipped; the recovery cascade falls through...".
- `src/lcsas/meta/builder.py:2041-2042` — `if not cache_root.is_dir(): return  # No cache →
  meta build still works for single-arch path.` (upstream rustic + python trees all skipped).
- `src/lcsas/meta/builder.py:2161-2162` — per-target tier-1: `if not src_bin.is_file():
  continue`.
- `src/lcsas/meta/builder.py:~2172-2233` — `_regenerate_recovery_manifest` recomputes all
  `./bin/` rows from disk contents; absent targets simply have no rows.
- `src/lcsas/cli/main.py:1976-2070` — `cmd_meta_verify` checks only entries listed in
  `recovery/MANIFEST.sha256`; `--strict` flags *extras*, never absences; root-level artifacts
  outside `recovery/` are never touched.
- `tests/unit/test_meta_builder.py:~564` — `test_no_cache_dir_is_silent_skip` pins the skip as
  intended ("Must not raise").
- `tests/recovery_hardening/test_meta_bundling_completeness.py` — checks
  `REPO_ROOT/recovery/bin/<short>/` (source tree, pre-build), tier-1 binaries only; runs via
  local `make test-recovery-hardening`; `.github/workflows/test.yml` runs unit+integration only.
- `recovery/docs/READINESS_CHECKLIST.txt:23-30` — boot test is manual and content-blind.

## Fix design

Introduce a single required-contents contract and enforce it at build AND verify.

**1. The contract** — new module `src/lcsas/meta/required_contents.py`:

```python
APPROVED_TARGETS = (...)  # six rust triples, single source of truth
def required_meta_paths() -> list[str]:
    """Relative paths every complete meta-volume must contain."""
    # per target: recovery/bin/<triple>/lcsas-restore[.exe],
    #             recovery/bin/<triple>/rustic[-static][.exe],
    #             recovery/bin/<triple>/python/ (tree marker: bin/python3 or python.exe)
    # root: standalone_restorer.py, keyshare_combine.py, START_HERE.txt,
    #       recovery/scripts/restore.sh, restore.bat, tools/
```
Import `APPROVED_TARGETS` into `builder.py`'s `tier1_map` and
`test_meta_bundling_completeness.py`'s list so they cannot drift.

**2. Build gate** — `cmd_meta_build` gains `--require-complete` (default **ON**; escape hatch
`--allow-incomplete` for single-arch dev builds, mirroring the existing
`LCSAS_OPTIONAL_ARCHES` convention). After `build()` finishes, walk
`required_meta_paths()` against the output dir; on any absence raise listing **every** missing
artifact grouped by target:
`"Meta-volume is INCOMPLETE — missing: [x86_64-pc-windows-gnu] lcsas-restore.exe, rustic.exe ...
Build the binaries (make keyshare-arches / make build-recovery, recovery/scripts/fetch_upstream.sh)
or pass --allow-incomplete for a dev build."`

**3. Verify gate** — `cmd_meta_verify`: after the manifest hash pass, run the same
required-contents check rooted at the output dir, reporting `ABSENT <path>` lines separately
from hash `MISMATCH`/`MISSING` (manifest-listed) lines, and return 1. This makes verify catch
both "never bundled" and "manifest self-referentially regenerated". Root-level artifacts get
covered by this existence check (hashing them can come later with a top-level manifest; the
contract check is the load-bearing part).

**Compat note:** verification of *old* (pre-gate) meta discs will now report ABSENT for targets
they never shipped — that is the desired honest signal. Print the gate's vintage:
"this check reflects the 2026-06 required-contents contract; older discs may predate it."
No catalog/schema change (the meta disc carries no catalog.db).

## Tests & gates

- `tests/unit/test_meta_builder.py::test_require_complete_fails_listing_missing_targets` —
  build with empty `LCSAS_RECOVERY_CACHE` + default flags; assert raised error enumerates all 6
  triples; `--allow-incomplete` builds. Repurpose `test_no_cache_dir_is_silent_skip` to assert
  the skip happens only under `--allow-incomplete`. Always-on (`make test-unit`).
- `tests/unit/test_meta_builder.py::test_meta_verify_reports_absent_required_artifacts` —
  build a complete fake tree, delete `recovery/bin/x86_64-pc-windows-gnu/`; assert
  `cmd_meta_verify` returns 1 naming it (today it returns 0).
- Extend (don't duplicate) `tests/recovery_hardening/test_meta_bundling_completeness.py` to also
  run against a BUILT meta-volume output (env var pointing at the build dir), asserting per
  triple: `lcsas-restore[.exe]`, rustic binary, python tree, plus the root artifacts — all
  driven from `required_meta_paths()`.
- Makefile target `meta-gate`: `make fetch-recovery build-recovery && lcsas meta build
  --output <tmp> && lcsas meta verify --strict --output <tmp>` — release-prep gate; wiring into
  CI (tags / scheduled) is owned by the GATE plans, but the target must exist here.

## Acceptance criteria

- [ ] `lcsas meta build` with an empty binary cache fails by default, listing every missing
      artifact for all six targets; `--allow-incomplete` restores dev behavior.
- [ ] Deleting any required artifact from a built meta tree makes `lcsas meta verify` return 1
      and name it.
- [ ] `tier1_map`, the hardening test, and the build/verify gates all read one
      `APPROVED_TARGETS` constant.
- [ ] `make meta-gate` passes on a fully-provisioned host and fails when any binary is removed.
- [ ] `make test-unit && make lint && make typecheck` pass.

## Dependencies & related plans

- RST-04 Part 1 (zstandard bundle failure) is the same fail-loud philosophy inside
  `_bundle_tools`; land together if convenient.
- GATE plans: wiring `meta-gate` + recovery-hardening into CI; the macOS-binary-execution and
  Intel-Mac-binary plans determine when all six binaries actually exist in CI.
- UX/BOOT plans rely on `restore.bat`/`START_HERE.txt` being present — this gate enforces their
  presence (INFRA-01 covers exercising restore.bat).

## Effort

2 days: 1 impl (contract module + build/verify wiring + error wording), 1 tests + Makefile
target. No special environment (gate logic is testable with fake trees; the full `meta-gate`
needs the binary cache, available on this machine).

---
**Implemented:** 2026-06-13. As planned, with two scoping notes: (1) the bundler methods (`_bundle_upstream_binaries`/`_bundle_tier1_binaries`) keep their silent skip-if-absent tolerance; completeness is enforced one level up by the `cmd_meta_build` gate + `MetaVolumeBuilder.missing_required_contents()` and the `cmd_meta_verify` ABSENT check (both reading `required_meta_paths()`). (2) `meta verify` takes a positional output dir + `--strict` (not `--output`); `make meta-gate` uses that form.
