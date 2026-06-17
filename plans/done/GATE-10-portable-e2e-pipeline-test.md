# GATE-10: test-e2e hard-skips off the author's machine (/mnt/lcsas-data), so the gate reports green while running nothing

> **STATUS: RESOLVED** — landed in `c977b82` (tests+ci: make e2e pipeline test portable via LCSAS_E2E_BASE [GATE-10]); guarded by `tests/recovery_hardening/test_ci_workflow_parity.py`.

**Priority:** P2 · **Severity:** low · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Make e2e pipeline test portable via LCSAS_E2E_BASE

## Problem

`make test-e2e` is part of `test-all` and therefore of `make gate` ("the final
gate that says this build is shippable"). But the only pytest-collected file
in `tests/e2e/` (`test_scripts.py`; the blind-restore directory is
shell-driven, not collected) skips unless the machine-specific
`/mnt/lcsas-data` LV exists — `scripts/e2e_test.py` hardcodes it as a
module-level constant with no override. On any other machine, and in CI if it
were ever added, the e2e tier passes by executing zero assertions: green-by-
skip, with a suite name that implies pipeline-level protection it doesn't
deliver. CI already installs real rustic + xorriso + dvdisaster, so the only
thing keeping the full scan→binpack→stage→ISO→ECC pipeline out of CI is this
hardcoded path.

## Evidence

Re-checked 2026-06-10 against master:

- `tests/e2e/test_scripts.py:39-42` — `@pytest.mark.skipif(not
  Path("/mnt/lcsas-data").exists(), reason="requires /mnt/lcsas-data LV...")`
  (second skipif at line 62 for the cdemu smoke script).
- `scripts/e2e_test.py:39` — `BASE = Path("/mnt/lcsas-data")`; lines 40-46
  derive MIRROR_DIR/STAGING_DIR/ISO_DIR/etc. from it at module level; no
  `--base` flag.
- `Makefile:26-27, 39, 44` — `test-e2e` → `test-all` → `gate`.

## Fix design

1. **Parameterize the base.** In `scripts/e2e_test.py`, replace the
   module-level constants with
   `BASE = Path(os.environ.get("LCSAS_E2E_BASE", "/mnt/lcsas-data"))` and
   derive the rest as before (keep them module-level — the script is a
   standalone procedure, no need to restructure). Default preserves the dev
   VM workflow (and the /scratch-vs-/ partition convention there).
2. **Replace the path-skip with a capability skip.** In
   `tests/e2e/test_scripts.py::test_e2e_pipeline`: compute the effective base
   (env or default); if it doesn't exist, fall back to
   `/var/tmp/lcsas-e2e-<pid>` and pass it via env to the subprocess; skip
   only when `shutil.disk_usage` shows < ~2 GiB free (TEST_TINY media keeps
   the footprint small). The existing `requires_rustic`/`requires_xorriso`
   markers keep tool-skips honest. Clean the tmp base in a finally block.
   Leave the cdemu smoke test's skip as-is (cdemu can't run in CI).
3. **Run it in CI.** Add to `.github/workflows/test.yml` (or fold into
   GATE-02's job): `LCSAS_E2E_BASE=/var/tmp/lcsas-e2e make test-e2e`.
   Expected runtime 1-2 min with TEST_TINY and the binaries CI already
   installs. Assert it actually ran: the step greps the pytest summary for
   `1 passed` on `test_e2e_pipeline` (green-by-skip is the bug being fixed —
   don't reintroduce it in CI).

No catalog/schema impact (the script builds and discards its own catalog).

## Tests & gates

- `tests/e2e/test_scripts.py::test_e2e_pipeline` — now executes on any
  machine with rustic+xorriso and 2 GiB free; always-on in `make gate`.
- CI step in test.yml with the passed-not-skipped check.
- One-time: run on a machine without the LV (or `LCSAS_E2E_BASE=/var/tmp/x`)
  and confirm the full pipeline passes; run on the dev VM unset and confirm
  it still uses the LV.

## Acceptance criteria

- [ ] `make test-e2e` on a host without /mnt/lcsas-data reports
      `test_e2e_pipeline PASSED` (not skipped).
- [ ] CI executes the pipeline test on every push; a deliberately broken
      `scripts/e2e_test.py` step turns CI red.
- [ ] Dev-VM behavior unchanged when LCSAS_E2E_BASE is unset and the LV
      exists.

## Dependencies & related plans

- **GATE-02** (hardening/e2e CI job) — lists test-e2e as KNOWN_UNWIRED until
  this lands; this plan unblocks that entry.
- **BURN-*** pipeline fixes — this test is the natural home for cheap
  regression checks they add; keeping it CI-run multiplies their value.

## Effort

1 day: 0.5 script/test refactor, 0.5 CI iteration (disk/space/runtime tuning
on hosted runners). No special environment.

---
**Implemented:** 2026-06-13. As planned, with two deviations: (1) `scripts/e2e_test.py` auto-creates the base only when `LCSAS_E2E_BASE` is explicitly set (the default LV must still be provisioned out-of-band); (2) the test's tmp-base cleanup uses a `_force_rmtree` that chmods the read-only directories xorriso extracts from the ISO before removal, since `ignore_errors`/file-only chmod left the scratch tree behind. CI runs the pinned `pytest .../test_e2e_pipeline` form (not `make test-e2e`) with a `grep '1 passed'` guard; `test_ci_workflow_parity.py` updated accordingly (KNOWN_UNWIRED now empty, EQUIVALENCE entry added). Verified off-LV (LCSAS_E2E_BASE absent + non-existent) and on-LV (unset) — both pass and the fallback leaves no stray dirs.
