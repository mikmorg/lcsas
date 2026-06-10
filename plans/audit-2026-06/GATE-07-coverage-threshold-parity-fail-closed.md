# GATE-07: CI runs the tier-1 coverage gate at 60% against a documented 88% floor, and the checker fails open on an empty report

**Priority:** P1 · **Severity:** medium · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Raise CI audit-gate threshold to measured floor; make coverage_check fail closed

## Problem

The tier-1 C binary's coverage contract is THRESHOLD=88 — `recovery/docs/AUDIT.md`
calls 88% "the measured floor... Prevents regressions", and both Makefiles
default to it. But the CI workflow explicitly overrides it:
`make -C recovery audit-gate THRESHOLD=60`. A per-file coverage collapse of up
to ~28 points in the code that authenticates and restores the heir's data
merges green, while AUDIT.md tells readers (twice, contradicting itself once)
that CI enforces "the default threshold". The contract exists only for
developers who happen to run `make audit-gate` locally.

Worse, the checker fails open: `coverage_check.py` returns **exit 0** with
only a stderr WARNING when zero `src/lcsas-restore/*.c` entries appear in
`coverage.json`. Any gcovr filter/path/flag drift that empties the report
silently disables the entire threshold gate — and project history records
exactly this failure mode once already (gcovr flag drift breaking coverage-c).
The combination means the one CI gate guarding tier-1 quality can be reduced
to a no-op by an innocuous tooling change, with green checks throughout.

## Evidence

Re-checked 2026-06-10 against master:

- `.github/workflows/audit-gate.yml:45-46` — step "Run audit-gate (default
  60% threshold)": `make -C recovery audit-gate THRESHOLD=60`.
- `Makefile:211` and `recovery/Makefile:827` — `THRESHOLD ?= 88`.
- `recovery/docs/AUDIT.md:35` — table row: "88% (default) | Measured floor
  after Phase 9 ... Prevents regressions."
- `recovery/docs/AUDIT.md:320` — "It runs `make -C recovery audit-gate` with
  the default threshold." (false — CI passes 60); `AUDIT.md:11` separately
  claims "default threshold 60%" — internally inconsistent with line 35.
- `recovery/scripts/coverage_check.py:75-77` —
  `if not rows: print("[coverage_check] WARNING: no src/lcsas-restore/*.c
  files found in report", ...); return 0` — fail-open.
- `audit-gate.yml:36-42` — comment documenting that CI coverage lands ~5 pts
  below local (the conftest/lcsas-install effect), which is the basis for the
  CI threshold below.

## Fix design

1. **Fail closed on an empty report.** `recovery/scripts/coverage_check.py`
   lines 75-77: change to

   ```python
   if not rows:
       print("[coverage_check] ERROR: no src/lcsas-restore/*.c files found "
             "in report — gcovr filter/path drift has emptied the coverage "
             "report; the threshold gate cannot run. Failing closed.",
             file=sys.stderr)
       return 1
   ```

2. **Raise the CI threshold to the measured floor.** In
   `.github/workflows/audit-gate.yml:46`, set `THRESHOLD=83` and rename the
   step "Run audit-gate (CI threshold 83 = local 88 floor − 5 pt measured CI
   delta)". Add a comment pointing at the lines 36-42 explanation and at
   `recovery/Makefile:827` as the authoritative local floor. If the first CI
   run shows the real delta differs, set the highest reliably-passing value
   and record the measurement in the comment — the point is a *derived*
   number with a stated relationship to 88, not another magic constant.

3. **Fix the docs.** `recovery/docs/AUDIT.md:320`: "CI runs
   `make -C recovery audit-gate THRESHOLD=83` — the local 88% floor minus the
   measured ~5 pt CI environment delta." Reconcile `AUDIT.md:11` ("default
   threshold 60%") with the line-35 table (88 is the default).

4. **Pin the parity so it can't drift back.** New
   `tests/recovery_hardening/test_audit_gate_threshold_parity.py`:
   - parse `THRESHOLD=(\d+)` from audit-gate.yml's run line and
     `THRESHOLD \?= (\d+)` from `recovery/Makefile`;
   - assert `ci >= local - 5` (tolerance constant with a comment);
   - assert the AUDIT.md sentence quotes the same CI number (same pattern as
     the existing doc-parity hardening tests).

No catalog/schema impact.

## Tests & gates

- `tests/unit/test_coverage_check_fail_closed.py` — runs
  `recovery/scripts/coverage_check.py` as a subprocess against three crafted
  `coverage.json` fixtures: (a) no lcsas-restore entries → exit 1 with the
  ERROR string; (b) one file below threshold → exit 1 naming it; (c) all
  above → exit 0. Always-on in `make test-unit` (already in CI).
- `tests/recovery_hardening/test_audit_gate_threshold_parity.py` — always-on
  in `make test-recovery-hardening`; CI-enforced once GATE-02 lands.
- The audit-gate workflow itself: one scratch run after the change to confirm
  83 passes on hosted runners (trigger via a whitespace edit under
  `recovery/src/lcsas-restore/`).

## Acceptance criteria

- [ ] `python3 recovery/scripts/coverage_check.py --threshold 88 --json
      empty.json` (no matching files) exits 1.
- [ ] audit-gate.yml passes `THRESHOLD=83` (or measured floor) and the
      workflow is green on master.
- [ ] AUDIT.md lines 11/35/320 agree with each other and with the workflow.
- [ ] Parity test fails on a scratch branch that sets `THRESHOLD=60` back.

## Dependencies & related plans

- **GATE-04** (audit-gate path-filter holes) — same workflow file,
  independent change; land in either order.
- **GATE-02** (hardening suite in CI) — makes the parity test merge-blocking.
- Coordinate with **KEY-06** (keyshare under coverage-c) — when keyshare files
  enter the report, the `src/lcsas-restore/` filter in coverage_check.py
  needs widening; keep the fail-closed branch covering the widened set.

## Effort

1 day: 0.25 checker + unit tests, 0.25 workflow/docs, 0.5 measuring the real
CI floor (one or two scratch-branch audit-gate runs). No special environment.
