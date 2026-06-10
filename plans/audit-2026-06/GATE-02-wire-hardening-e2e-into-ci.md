# GATE-02: the "shippable build" gate (recovery-hardening + e2e) never runs in CI

**Priority:** P1 · **Severity:** high · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: CODE_REVIEW_CLEANUP.md:172-181 (generic "[ ] Tests run in CI" boxes only; the hardening/CI split is not called out)
**Suggested GH issue title:** Run recovery-hardening suite (and later test-e2e) in CI on every push

## Problem

The project defines `make gate` — lint + typecheck + unit + integration + e2e
+ recovery-hardening — as "the final gate that says this build is shippable",
and the recovery-hardening suite (58 Python files, ~55 test modules) is where
all behavioral tests of `recovery/scripts/restore.sh` live: tier fallback,
disc discovery, tier-3 disc swap, doc-parity, UX checks. But CI
(`.github/workflows/test.yml`) runs only four steps: test-unit,
test-integration, typecheck, lint. There is no pre-push hook either
(`.git/hooks` contains only samples). The shippability bar is enforced purely
by local discipline.

The only restore.sh-related coverage that runs on merge is
`tests/unit/test_restore_sh_dispatcher.py` — which executes a *hand-copied
mirror* of the dispatcher case block ("MUST stay in lockstep" comment) plus a
string-pinning drift guard, never the script itself — and a `bash -n` syntax
check in `tests/unit/test_meta_builder.py`. Combined with the audit-gate path
filter excluding `recovery/scripts/` entirely (GATE-04), a behavioral
regression to the heir's single entry-point script can merge with zero CI
execution of that script. For a system whose whole promise is that the
recovery path keeps working for decades, the most heir-relevant test tier is
the one with no machine enforcement.

## Evidence

Re-checked 2026-06-10 against master:

- `.github/workflows/test.yml:78-88` — exactly four gate steps (`make
  test-unit`, `make test-integration`, `make typecheck`, `make lint`).
- `Makefile:36-39` — "the hardening checks are the final gate that says 'this
  build is shippable.'"; `Makefile:44` — `gate: lint typecheck test-all`;
  `Makefile:39` — `test-all: test-unit test-integration test-e2e
  test-recovery-hardening`.
- `.git/hooks/` — sample hooks only; nothing enforces `make gate` pre-push.
- `tests/unit/test_restore_sh_dispatcher.py:35-37` — "the case block here MUST
  stay in lockstep with the one in recovery/scripts/restore.sh".
- `ls tests/recovery_hardening/*.py | wc -l` → 58.
- Verifier nuances (confirmed): `tests/integration/test_meta_volume_restore.py`
  executes `restore_legacy.sh`, not the active 3-tier driver;
  `test_interactive_restore.py` drives production restore.sh but is root-gated
  so it skips in CI; audit-gate's coverage-c Step 3 runs just 3 tier-1
  hardening files with `|| true` (recovery/Makefile:~507).

## Fix design

**1. New `recovery-hardening` job in `.github/workflows/test.yml`** (separate
job, parallel to `test`, so a hardening failure is legible at a glance):

```yaml
recovery-hardening:
  runs-on: ubuntu-latest
  env: { RUSTIC_VERSION: ..., RUSTIC_ASSET: ..., RUSTIC_SHA256: ... }  # same pins as `test`
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: "3.11" }
    - name: Install rustic (pinned)        # copy of the existing step; extract to a
      run: ...                             # composite action if duplication grates
    - name: Install xorriso + dvdisaster + qemu + wine
      run: sudo apt-get install -y xorriso dvdisaster qemu-user-static wine
    - name: Install LCSAS
      run: make dev
    - name: Build host tier-1 binaries + C unit tests
      run: make -C recovery all test       # ~2 min; gives the tier-1 hardening
                                           # tests a build/lcsas-restore to target
    - name: Recovery hardening suite
      run: pytest tests/recovery_hardening/ -v --junitxml=/tmp/hardening.xml
    - name: Skip-rot floor
      run: python3 scripts/ci_min_passed.py /tmp/hardening.xml --min-passed "$FLOOR"
```

Tests needing root (4 files), cdemu (5 files), or live ISOs already skip
honestly — that is fine, *as long as skips can't silently grow*. The
**skip-rot floor** is the key piece: `scripts/ci_min_passed.py` (~30 lines)
parses the junit XML and fails if `passed < --min-passed`. Set `FLOOR` from
the first green CI run minus a 5-test margin, with a comment in the workflow
explaining how to re-baseline. Installing `qemu-user-static` and `wine` lets
the aarch64/armv7/windows committed-binary tests actually run in CI (they
currently skip everywhere but this VM), raising the floor meaningfully.

**2. `make test-e2e` step — deferred behind GATE-10.** `tests/e2e/test_scripts.py`
hard-skips off-host today (`/mnt/lcsas-data`); wiring it into CI before
GATE-10's portability fix would add a permanently-skipping step and false
confidence. Add the step in the same job once GATE-10 lands.

**3. Workflow-parity gate** so the suite list can never silently drift again:
new `tests/recovery_hardening/test_ci_workflow_parity.py`:

- Parse `Makefile` to collect the transitive prerequisites of `gate`
  (lint, typecheck, test-unit, test-integration, test-e2e,
  test-recovery-hardening — plus `shell-coverage` after GATE-09).
- Parse `.github/workflows/test.yml` (plain-text contains-check is enough; no
  YAML lib needed) and assert each prerequisite appears as `make <target>` or
  an equivalent pinned command (maintain an explicit, commented equivalence
  map for e.g. the raw `pytest tests/recovery_hardening/` invocation).
- Allow a pinned `KNOWN_UNWIRED` set (initially `{"test-e2e"}`, removed by
  GATE-10) so the gap is *declared*, not invisible.

**4. Optionally** add a `pre-push` hook installer (`make hooks`) running
`make gate` — cheap, but CI is the enforcement that matters; do not let the
hook substitute for the CI job.

No catalog/schema impact.

## Tests & gates

- CI job above: always-on, every push/PR. Expected wall time ~8-12 min
  (suite is dominated by subprocess-driving tests; measure on first run).
- `scripts/ci_min_passed.py` — unit-test it with a synthetic junit file
  (`tests/unit/test_ci_min_passed.py`: below-floor fails, at-floor passes).
- `tests/recovery_hardening/test_ci_workflow_parity.py` — always-on; fails if
  someone adds a tier to `test-all` without wiring CI, or deletes the CI step.
- Existing partial coverage retained: dispatcher mirror test, `bash -n`
  syntax check, audit-gate Step 3 — none removed.

## Acceptance criteria

- [ ] A PR that breaks a restore.sh behavior pinned by
      `tests/recovery_hardening/test_restore_sh_ux.py` (e.g. mangle a prompt
      string) goes red in CI.
- [ ] CI run shows hardening passed-count ≥ FLOOR; deliberately skipping 10
      tests (e.g. uninstall wine in a scratch branch) goes red via the floor.
- [ ] `test_ci_workflow_parity` fails when the hardening step is deleted from
      test.yml (verified once on a scratch branch).
- [ ] `make gate` locally and CI run the same suite list (modulo declared
      `KNOWN_UNWIRED`).

## Dependencies & related plans

- **GATE-10** (portable e2e pipeline test) — prerequisite for the `test-e2e`
  CI step; until then it sits in `KNOWN_UNWIRED`.
- **GATE-09** (shell-coverage wiring) — appends `make shell-coverage` to this
  job and to `gate`.
- **GATE-01** (Intel-Mac binary) — its interim test.yml step folds into this job.
- **GATE-04** (audit-gate path filter) — complementary: GATE-04 gates C paths,
  this plan gates script/Python behavior.
- **INFRA-01** (Windows-e2e CI scaffolding) and **UX-08** (cross-OS journey
  gates) — same spirit for the other OSes; this plan is the Linux leg.

## Effort

2 days: 0.5 impl (workflow + floor script), 0.5 parity test, 1.0 iterating CI
(first runs will surface env-specific skips/failures; budget for 3-5 red
runs). Needs nothing beyond GitHub-hosted ubuntu runners.
