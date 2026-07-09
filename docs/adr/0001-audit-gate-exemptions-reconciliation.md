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

## Outcome (2026-07-08)

Executed on branch `fix/383-audit-exemptions`.

**Diagnosis refined.** The failure was overwhelmingly **line-number drift**,
not new gaps: #384 (and other post-06-17 merges) shifted `repo.c`/`main.c`/
`tree.c` line numbers, so an entry like `repo.c:369 decrypt-MAC-fail` now
describes a comment while the branch it documented moved to ~463. The
authoritative worklist came straight from the failing audit-gate CI log
(`exemptions_check` prints the exact sets): **133 undocumented + 95 stale →
187 currently-uncovered lines**. Reconciling to CI's set is *more* correct
than a local sweep — the new per-run `timeout` makes local coverage ≤ CI's, so
reconciling to a local run would over-document and re-red on `covered_but_listed`.

**Hybrid discipline (test vs document).** Only genuinely *net-new*
(undocumented) lines got the write-a-test treatment:
- `main.c` env-override + wrong-password branches — 2 fixture-only tests in
  `test_tier1_unit.py` plus wiring `test_tier1_fault_handling.py` into
  coverage-c's Step-3 pytest list (both fixture-only, no rustic).
- `tree.c` root-not-object / symlink-embedded-NUL(security) / "too large" (-2) —
  3 new `test_tree.c` cases via the existing stub harness.
- `repo.c:175` and `repo.c:1092-1097` are then closed *incidentally* by the
  now-wired `test_tier1_fault_handling` (truncated key / truncated pack).

**Quality finding — the contract was partly a dumping ground.** A source-anchored
re-classification of every uncovered line found many entries that are NOT
genuinely unreachable: the recurring `repo.c` "AEAD prevents crafting without
breaking the primitive" rationale is **false** — the C unit harness controls the
master key (`test_repo.c enc_write`) and the blob metadata (`lcsas_blob_loc`), so
a MAC-fail / hash-mismatch / corrupt-zstd input is crafted by corrupting a valid
blob, no primitive broken. Rather than write ~40 crypto-craft tests inside #383
(out of scope), their **category** is corrected from the false `INTRACTABLE` to
the honest `DEFERRED` with a group rationale and the test-conversion tracked in
**#401**. This is doc-only and changes no gate behaviour (the check ignores
category). `repo.c:461-463` was corrected `INTRACTABLE → DEFENSIVE` (the
`strip_v2_prefix` 0-byte return is provably unreachable — decrypt rejects <33 B).

**Reconciled fence: 159 rows** (was 149 drifted) — INTRACTABLE 61, DEFERRED 66,
DEFENSIVE 23, VOLATILE 9; 28 formerly-listed/net-new lines are now closed by
tests. The nine VOLATILE entries (readdir-order / fs-full, incl. `repo.c:216-218`)
were preserved on re-anchor so they don't re-red on whichever host covers them.

**Guard (Decision 3c) — `test_exemptions_contract.py` (GATE-11).** A stdlib-only,
offline meta-test in the *watched* suite (`make gate` + `test.yml`), following the
GATE-02/07/08 pattern. It pins the enforcement wiring (exemptions_check ∈
coverage-c ∈ audit-gate ∈ CI) so the gate can't be silently decoupled, and fence
well-formedness (valid categories, every pinned line exists in-file) so gross rot
surfaces in the watched suite. **Its limit is honest and documented:** it cannot
detect a *fresh line-drift* red — only a coverage run can. The complete fix for
that is making the audit-gate CI job a **required status check**; this is
*recommended* rather than done here because the pre-existing-red bin-parity gate
(#381/#320) still forces `--admin` merges that would bypass a required check too.
Flip it once #381 lands.
