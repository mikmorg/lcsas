# GATE-05: RS03 ECC repair proof is opt-in and never runs in CI

**Priority:** P1 · **Severity:** high · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: recovery/docs/READINESS_CHECKLIST.txt:95-101 + root CLAUDE.md record the opt-in status; the absence of any scheduled/CI execution is untracked
**Suggested GH issue title:** Run the RS03 ECC repair proof weekly in CI

## Problem

The disc-integrity layer's core promise — below-threshold disc damage repairs
byte-identical, above-threshold fails loud — is proven by exactly one test:
`tests/integration/test_ecc_repair.py`, which is skipped unless
`LCSAS_ECC_REPAIR=1`. CI installs dvdisaster but never sets the variable, so
the repair path is validated only when someone remembers to run it locally —
and project memory records it as slow and rarely run.

The regression window this leaves open is uniquely nasty for the heir goal: a
change to `src/lcsas/ecc/dvdisaster.py` (argument drift, wrong image targeted,
redundancy semantics change) would silently degrade *every disc burned
thereafter*. Nothing fails today; the failure surfaces decades later when the
heir's damaged disc cannot be repaired. A weekly scheduled run bounds that
window to days instead of years, at zero cost to per-push CI latency.

One audit proposal is already satisfied: pure argument drift IS caught per
push by `tests/unit/test_dvdisaster.py::test_augment_args`, which pins
`-mRS03`, `-n`, and the redundancy value against a mocked subprocess. What has
no tripwire is the real binary's end-to-end repair behavior.

## Evidence

Re-checked 2026-06-10 against master:

- `tests/integration/test_ecc_repair.py:41-46` —
  `pytest.mark.skipif(not os.environ.get("LCSAS_ECC_REPAIR"), ...)`; module
  docstring (line 17-19) documents the opt-in invocation.
- `.github/workflows/test.yml:61-66` — installs dvdisaster;
  `grep -r LCSAS_ECC_REPAIR .github/ Makefile` → no hits: never set in CI or
  any make target.
- `tests/unit/test_dvdisaster.py:14-26` — `test_augment_args` pins
  `-mRS03` / `-n` / `20` (CI-run, mocked). All other DVDisaster-touching
  tests inject `_NoOpDVDisaster`-style fakes, so the real binary's repair
  path executes nowhere in CI.

## Fix design

**New `.github/workflows/ecc-weekly.yml`:**

```yaml
name: ecc-weekly
on:
  schedule:
    - cron: "0 4 * * 1"          # Mondays 04:00 UTC
  workflow_dispatch: {}
  pull_request:
    paths:
      - "src/lcsas/ecc/**"
      - "tests/integration/test_ecc_repair.py"
      - ".github/workflows/ecc-weekly.yml"
jobs:
  ecc-repair:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: sudo apt-get update && sudo apt-get install -y xorriso dvdisaster
      - run: make dev
      - name: RS03 repair proof (below-threshold repairs byte-identical; above fails loud)
        run: LCSAS_ECC_REPAIR=1 pytest tests/integration/test_ecc_repair.py -v -m integration
```

Details that matter:

- The `pull_request.paths` trigger gives immediate coverage for changes to
  the ECC wrapper itself — the highest-risk edits don't wait for Monday.
- Scheduled workflows run only against the default branch (master) — correct
  here; the schedule exists to catch environment/toolchain drift (e.g. an
  ubuntu dvdisaster package bump changing behavior), not branch work.
- Failure visibility: GitHub emails the workflow-file author on scheduled
  failures, but add an explicit final step `if: failure()` that creates/
  comments a GitHub issue (`gh issue create --title "ecc-weekly failed" ...`
  with `GH_TOKEN: ${{ github.token }}`) so a red Monday run can't be missed.
- Keep the test itself opt-in for local default runs (per project memory:
  slow, don't run in the default suite on this VM) — the workflow opts in
  explicitly; no change to the skipif.
- Per the verifier: do NOT add another golden-args unit test —
  `test_augment_args` already covers that; extend it only if a specific flag
  turns out to be unpinned while implementing this.

**Doc touch-ups:** update `recovery/docs/READINESS_CHECKLIST.txt:95-101` and
the CLAUDE.md test-environment note to state the proof now runs weekly in CI
(`ecc-weekly` workflow) in addition to the local opt-in path.

No catalog/schema impact.

## Tests & gates

- `ecc-weekly.yml` — scheduled weekly + manual dispatch + on-PR for ECC
  paths. The job runs the existing `test_ecc_repair.py` unchanged (it already
  asserts byte-identical repair below threshold and loud failure above).
- First-run validation: trigger via `workflow_dispatch`, confirm green and
  record wall time; if it exceeds ~40 min on hosted runners, reduce the
  damage-sweep size via an env knob in the test rather than dropping cases.
- Negative validation once (scratch branch): invert a dvdisaster argument in
  the wrapper, dispatch the workflow, confirm red + auto-issue creation.

## Acceptance criteria

- [ ] `gh workflow run ecc-weekly` completes green on master.
- [ ] A PR touching `src/lcsas/ecc/` triggers the job automatically.
- [ ] A deliberate wrapper regression turns the job red and files an issue.
- [ ] READINESS_CHECKLIST.txt no longer describes the repair proof as
      local-opt-in-only.

## Dependencies & related plans

- **FMT-01** (RS03 repair in-housing decision) — when repair moves in-house,
  extend this workflow with the bundled-tools-only damaged-disc e2e (corrupt
  an augmented image, repair using only what the meta-volume bundles). Keep
  the dvdisaster-based proof running regardless: the burned back-catalog's
  parity was written by dvdisaster.
- **BURN-07** (RS03 redundancy knob + preflight) — changes the wrapper's
  argument surface; this workflow is its safety net, land GATE-05 first.
- **GATE-02** — independent; this stays a separate workflow because of its
  runtime.

## Effort

1 day: 0.25 workflow, 0.25 issue-on-failure step, 0.5 first-run iteration and
runtime measurement on hosted runners. No special hardware (works on a plain
runner — the test damages ISO files, not physical discs).
