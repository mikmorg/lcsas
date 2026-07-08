# ADR 0001 — Reconciling the tier-1 audit-gate coverage-exemptions contract (#383)

- Status: Accepted (plan)
- Date: 2026-07-08
- Issue: #383

## Context

`.github/workflows/audit-gate.yml` (the "Tier-1 audit gate") has been RED on
`master` on every run since 2026-06-17. It went unnoticed for weeks because
`make gate` — the local pre-merge gate — does **not** invoke `audit-gate`
(it is a separate, ~40-minute target). This is the same "silently red CI"
failure mode as #378 (test.yml) and #381 (bin-parity).

Investigation corrected the issue's original framing:

- The failing step is `coverage-c` → `exemptions_check.py`, **not** a coverage
  shortfall. Measured tier-1 line coverage is **88.5%**, comfortably above the
  gate threshold (83% CI / 88% local floor). *Do not lower the threshold.*
- `recovery/docs/EXEMPTIONS.md` is a **live contract**: every uncovered line in
  `recovery/src/lcsas-restore/*.c` must be listed, and every listed line must
  actually be uncovered (VOLATILE excepted). The contract has **drifted**:
  ~133 uncovered lines are undocumented and ~95 entries now refer to lines that
  are covered. The drift is repo-wide since 06-17 and was widened by legitimate
  source edits (e.g. #384's `repo.c`/`main.c` changes shifted line numbers).
- `exemptions_check` runs on **CI** against **CI's** `coverage.json`, so the doc
  must match CI's uncovered set — not necessarily a local run's.
- Accurate local coverage is blocked: `run_coverage_fault_inject.sh` sweeps each
  malloc-injection point with **no per-run timeout**, and one `test_ecc`
  injection wedges locally (CI grinds through it in ~15 min).

## Decision

1. **Reconcile the whole contract, hybrid discipline.** For each undocumented
   uncovered line: if a test can cheaply reach it (a normal error path), write
   the test; if it is genuinely unreachable in-harness (allocation failure,
   ENOSPC/EDQUOT, root-only `chown`, `readdir`-order branches), document it with
   the correct category. Remove/fix the ~95 stale entries. EXEMPTIONS.md stays a
   list of *genuinely* unreachable code, not a dumping ground.
2. **Fix the sweep, iterate locally, confirm on CI.** Add a per-run `timeout` to
   `run_coverage_fault_inject.sh` so a wedged injection is killed and the sweep
   completes locally. Reconcile against local `coverage.json` (re-running the
   fast `exemptions_check.py` in seconds; re-running the full sweep only when
   tests change), then push and fix-forward the small residual where local != CI
   (timeout-killed / VOLATILE-order lines).
3. **Definition of done = green + documented + guarded.** (a) audit-gate CI job
   green; (b) this ADR; (c) a lightweight guard so audit-gate cannot silently
   re-red for weeks (surface staleness in `make gate` and/or make the CI check
   required).

## Exemption taxonomy (glossary)

- **INTRACTABLE** — cannot be tested without infrastructure beyond the harness
  (signal injection, breaking a MAC, etc.).
- **DEFENSIVE** — provably unreachable given upstream invariants; kept for
  safety/readability.
- **DEFERRED** — tractable but cost > value (e.g. 1M-file fixtures); documented
  for a future contributor.
- **VOLATILE** — environment/order-dependent (e.g. `readdir` order): covered on
  some hosts, uncovered on others. Stays listed (satisfies "every uncov line
  listed") but is exempt from the "listed-but-covered → remove" rule.

## Consequences

- The gate regains meaning: recovery/src PRs stop needing `--admin` for
  audit-gate. (bin-parity, #381/#320, remains a separate `--admin` reason.)
- A per-run sweep timeout means a genuinely non-terminating fault-injection is
  masked for coverage purposes; the specific wedging `test_ecc` point is noted
  as a low-priority diagnostic (possible real infinite-retry/deadlock on
  allocation failure) but does not block #383.
- EXEMPTIONS.md is MANIFEST-pinned; its row is refreshed on every edit.
