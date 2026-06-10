# GATE-09: shell-coverage gate for restore.sh enforces 60% while documenting 90%, swallows pytest failures, and is wired into nothing

**Priority:** P2 · **Severity:** medium · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix shell-coverage threshold drift and wire it into gate + CI

## Problem

`recovery/scripts/restore.sh` is the heir's single entry point, and
`make shell-coverage` (issue #213) is its only line-coverage floor. The target
is defanged three ways: the Makefile comment promises "Threshold: 90%" but the
invocation passes `--threshold 60`; the pytest run that *generates* the trace
ends in `|| true`, so a collection crash produces a near-empty trace that the
gate cannot distinguish from "tests passed, low coverage" until coverage
happens to dip under 60; and the target is a dependency of neither `make gate`
nor any CI workflow. Net: a large untested branch in restore.sh (e.g. a new
tier-dispatch path) can land with the trace silently shrinking, and nobody
runs the check anyway unless they invoke it by name.

This matters more after the audit than before it: several P0/P1 plans (UX
phantom-flag fixes, KEY share-card flows, GATE-06's tier1-missing chain) add
or change restore.sh branches. A real, enforced coverage floor is the cheap
guard that those branches arrive with tests.

## Evidence

Re-checked 2026-06-10 against master (root `Makefile`):

- `Makefile:57` — comment: "Threshold: 90% (set via --threshold to fail the
  target if lower)."
- `Makefile:68-70` — `pytest tests/recovery_hardening/test_restore_*.py
  ... -q || true` (trace-generation run; failures swallowed).
- `Makefile:71-72` — `python3 tools/cov_shell.py --threshold 60 ...`.
- `Makefile:44` — `gate: lint typecheck test-all` — no shell-coverage.
- `.github/workflows/test.yml` — no shell-coverage step (four steps only,
  lines 78-88).
- `tools/cov_shell.py` exists and honours `--threshold` (used today at 60).

## Fix design

All changes in the root `Makefile` plus one parity test; CI wiring rides
GATE-02's job.

1. **Fail loud on trace generation.** Drop `|| true` from the pytest run
   (Makefile:70). pytest exit 0 = pass; any non-zero (collection error,
   test failure, exit 5 "no tests collected") must fail `shell-coverage` —
   a broken trace run is precisely the state the gate exists to catch.
   Honest per-test skips (missing optional tools) remain fine; they exit 0.

2. **Reconcile the threshold with reality.** Run `make shell-coverage` on
   master, record the measured percentage, and set `--threshold` to that
   floor (capped at 90 if it already exceeds it); rewrite the Makefile:57
   comment to state the actual number and the ratchet intent ("raise toward
   90 as branches gain tests; never lower without an issue"). Choosing the
   measured floor over a flat 90 keeps the gate green-on-master from day one
   — a gate that starts red gets bypassed, not fixed.

3. **Wire into the local gate.** `Makefile:44`:
   `gate: lint typecheck test-all shell-coverage`. Cost: it re-runs the
   restore.sh hardening subset (~1-2 min) with tracing; acceptable for the
   "shippable" bar the gate claims to be.

4. **Wire into CI.** Append `make shell-coverage` to GATE-02's
   `recovery-hardening` job (after the suite run, same checkout; bash is the
   runner default). Until GATE-02 lands, add it as a standalone test.yml step
   — it is self-contained (pytest + bash + tools/cov_shell.py).

5. **Pin comment/flag parity.** Extend the doc-parity pattern: a small test
   `tests/recovery_hardening/test_shell_coverage_contract.py` parses the
   Makefile's `shell-coverage` recipe, asserts the "Threshold:" comment value
   equals the `--threshold` flag value, and asserts `|| true` does not appear
   in the recipe. Kills both drifts permanently.

No catalog/schema impact.

## Tests & gates

- `make shell-coverage` — becomes a real gate: fails on pytest non-zero OR
  coverage below the documented floor. Part of `make gate` (always-on
  locally) and of the GATE-02 CI job (always-on on push/PR).
- `tests/recovery_hardening/test_shell_coverage_contract.py` — always-on;
  fails if comment and flag diverge or `|| true` returns.
- One-time verification: on a scratch branch, add an unreachable `elif`
  branch with 5 dead lines to restore.sh → `make shell-coverage` goes red;
  break a hardening test's import → goes red (previously: green until <60).

## Acceptance criteria

- [ ] `make shell-coverage` exits non-zero when the pytest trace run fails
      at collection (verified via scratch-branch syntax error in a test).
- [ ] Makefile comment threshold == `--threshold` flag == measured floor;
      contract test passes and fails appropriately on a scratch divergence.
- [ ] `make gate` runs shell-coverage; CI runs it on every push/PR.
- [ ] Coverage floor documented with its measurement date in the Makefile
      comment.

## Dependencies & related plans

- **GATE-02** (recovery-hardening CI job) — preferred CI host for the step;
  this plan can land first with a standalone test.yml step and fold in later.
- **UX-02/KEY-02** (restore.sh doc/flag fixes) and **GATE-06** (tier1-missing
  chain) — add restore.sh branches; land this floor early so their diffs are
  measured against it.

## Effort

1 day: 0.25 Makefile edits, 0.25 measure + set floor, 0.25 contract test,
0.25 CI wiring + scratch-branch verification. No special environment (bash +
pytest only).
