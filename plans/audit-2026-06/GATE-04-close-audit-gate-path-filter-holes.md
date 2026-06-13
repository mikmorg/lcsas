# GATE-04: audit-gate path filter excludes vendored C, keyshare, and all recovery scripts

**Priority:** P1 · **Severity:** high · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Broaden audit-gate path filter and add an always-on C smoke build to CI

## Problem

The audit-gate workflow — the only CI that compiles or tests any C — triggers
on a hand-picked path list: `recovery/src/lcsas-restore/**`,
`recovery/tests/**`, `recovery/fuzz/**`, `coverage_check.py`, `sanitize.sh`,
`recovery/Makefile`. Everything else that ends up inside heir-facing binaries
or scripts is excluded:

- `recovery/vendored/sqlite/` and `recovery/vendored/zstd/` — C source
  compiled *directly into* the tier-1 binary; an edit there triggers no CI
  build, no test.
- `recovery/src/lcsas-keyshare/` — the heir-facing SLIP-0039 combiner. Its C
  test (`recovery/tests/test_keyshare.c`) runs only inside audit-gate, which a
  keyshare source change never triggers; the CI-run Python keyshare tests
  exercise only the Python combiner.
- `recovery/src/lcsas-iso9660/` and `recovery/src/lcsas-init/` — likewise.
- `recovery/scripts/` (restore.sh, restore.bat, restore_auto.sh,
  detect_arch.sh, fetch_upstream.sh, exemptions_check.py) — entirely
  unfiltered.
- The listed `recovery/scripts/sanitize.sh` does not exist — a dead filter
  entry that suggests the list has already drifted once unnoticed.

Since `test.yml` contains no C compilation step at all, a C-breaking change
outside `lcsas-restore/` produces a fully green CI: a behavior-breaking edit
to the vendored zstd decoder or the keyshare combiner merges unbuilt and
untested. These are exactly the components the non-technical heir's restore
runs decades from now.

## Evidence

Re-checked 2026-06-10 against master:

- `.github/workflows/audit-gate.yml:4-19` — push and pull_request path lists
  are exactly the six entries above.
- `ls recovery/scripts/` — no `sanitize.sh` (the sanitize *target* lives in
  recovery/Makefile; the script never existed under that name).
- `recovery/Makefile:121-135` — lcsas-keyshare built from
  `src/lcsas-keyshare/` (outside the filter); `recovery/Makefile:89` —
  `all:` builds lcsas-restore + lcsas-iso9660 + lcsas-init + lcsas-keyshare;
  `:278-279` — `test:` runs all 15 C unit-test binaries
  (`recovery/tests/test_*.c`).
- `grep -rln lcsas-keyshare tests/ --include='*.py'` → no Python test drives
  the C combiner binary; `tests/unit/test_keyshare_combine.py` tests
  `lcsas.meta.keyshare_combine` (pure Python).
- `.github/workflows/test.yml` — no `make -C recovery` step anywhere.

## Fix design

**1. Broaden the audit-gate filter** (both `push.paths` and
`pull_request.paths` in `.github/workflows/audit-gate.yml`):

```yaml
paths:
  - "recovery/src/**"
  - "recovery/tests/**"
  - "recovery/fuzz/**"
  - "recovery/vendored/**"
  - "recovery/scripts/**"
  - "recovery/Makefile"
  - "recovery/MANIFEST.sha256"
```

This subsumes and deletes the dead `sanitize.sh` entry and the
now-redundant `coverage_check.py` entry. Directory globs over file pins:
the file-pin approach is what drifted.

Note the filter only controls *when* audit-gate runs; coverage thresholds
still measure `src/lcsas-restore` only (KEY-06 extends coverage-c to
keyshare). That's acceptable: a vendored/keyshare change now at least
rebuilds everything and runs all C tests + sanitizers + fuzz-smoke.

**2. Always-on C smoke step in `test.yml`** — path filters can drift again;
a cheap unconditional build cannot:

```yaml
- name: C smoke (build all recovery binaries + run C unit tests)
  run: make -C recovery all test
```

~2 min with the stock gcc on ubuntu-latest (no clang/gcovr needed). Place it
in the existing `test` job after "Install LCSAS". After this lands, *no* C
change of any kind can merge unbuilt or with a failing C unit test —
regardless of what audit-gate's filter says.

**3. Ghost-entry and coverage gate** — new
`tests/recovery_hardening/test_workflow_path_filter.py` (always-on, no
external tools):

- Parse `audit-gate.yml` (text-level extraction of the `paths:` lists is
  fine). For every non-glob entry, assert the path exists in the repo —
  kills future ghost entries like `sanitize.sh`.
- Assert every directory under `recovery/src/` and `recovery/vendored/` is
  matched by at least one filter entry (simple `fnmatch` against the `**`
  globs) — so adding `recovery/src/lcsas-newtool/` without gate coverage
  fails the suite.
- Assert push and pull_request lists are identical (they must not drift
  from each other).

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_workflow_path_filter.py` — always-on once
  GATE-02 wires the hardening suite into CI; until then it still runs in
  `make gate` locally. Fails on master today (sanitize.sh ghost + unmatched
  dirs), passes after step 1.
- test.yml C smoke step — always-on, every push/PR.
- Verification on a scratch branch: touch `recovery/vendored/zstd/<file>.c`
  with a syntax error → C smoke goes red AND audit-gate triggers; previously
  both stayed green/silent.
- KEY-06's deeper gates (coverage-c filter, slip39 fuzz harness) layer on
  top; do not duplicate them here.

## Acceptance criteria

- [ ] audit-gate triggers on a PR touching only
      `recovery/vendored/sqlite/sqlite3.c` (verify via a whitespace-change
      scratch PR).
- [ ] audit-gate triggers on a PR touching only
      `recovery/src/lcsas-keyshare/slip39.c`.
- [ ] `make -C recovery all test` runs in every CI `test` job; a C syntax
      error anywhere under `recovery/src/` or `recovery/vendored/` turns CI
      red.
- [ ] `test_workflow_path_filter.py` passes; re-adding a nonexistent path to
      the filter fails it.

## Dependencies & related plans

- **KEY-06** (keyshare under coverage-c/fuzz/audit-gate) — this plan's filter
  fix is its trigger-wiring half; KEY-06 owns measurement depth.
- **GATE-02** (hardening suite in CI) — makes the path-filter test always-on
  in CI.
- **GATE-07** (threshold parity + fail-closed checker) — same workflow,
  independent fix; land in either order.
- **UX-02** (docs-vs-reality contract gate) — covers the *content* of
  recovery/scripts docs; this plan only ensures changes there trigger gates.

## Effort

1 day: 0.25 workflow edits, 0.25 C smoke step (verify gcc-only build works on
ubuntu-latest), 0.5 path-filter test + scratch-branch trigger verification.

---
**Implemented:** 2026-06-13. As planned, with two scope-accurate notes: (1) `recovery/src/lcsas-keyshare/**` was already in the audit-gate filter as a file-pin (added by KEY-06); the broadened `recovery/src/**` glob subsumes it. (2) The `recovery-hardening` CI job already ran `make -C recovery all test` (GATE-02); this plan adds the same smoke step to the always-on `test` job per the Fix design, so C build+unit-test coverage no longer depends on that one job. Path-filter test verified red-first against the pre-fix filter (ghost `sanitize.sh` + uncovered vendored/ecc/init/iso9660 dirs).
