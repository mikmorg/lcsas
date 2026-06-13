# KEY-06: lcsas-keyshare C combiner sits outside every tier-1 audit gate

**Priority:** P1 · **Severity:** medium · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: .claude/skills/key-escrow/PLAN.md C5.3 ([~] follow-up note; in no known-issues ledger)
**Suggested GH issue title:** Bring lcsas-keyshare under coverage-c, fuzzing, and audit-gate CI

## Problem

The C combiner parses untrusted, heir-typed text (mnemonics, and with KEY-01,
whole card files) on the 50-year critical path — but the project's tier-1
merge bar cannot see it. `coverage-c` filters to `src/lcsas-restore` only; all
five fuzz harnesses target lcsas-restore parsers (no SLIP-0039/mnemonic
fuzzer); EXEMPTIONS.md and AUDIT_FINDINGS.md never mention keyshare; and the
audit-gate CI workflow's path filter omits `recovery/src/lcsas-keyshare/**`,
so a PR touching only combiner source triggers no C gate at all. Today's
coverage is one 45-vector unit test (which runs in `make -C recovery test`,
but CI only reaches it when *other* recovery paths change) plus a one-off
manual ASan run recorded in PLAN.md.

PLAN.md C5.3 acknowledges this as a "documented follow-up" — this plan closes
it. It becomes more urgent with KEY-01/KEY-07, which add new parsing code to
exactly this directory.

## Evidence

Re-checked 2026-06-10 against master:

- `recovery/Makefile:26` — `SRCDIR = src/lcsas-restore`; `:543` —
  `gcovr … --filter '$(SRCDIR)/.*'` (coverage-c sees only lcsas-restore).
- `recovery/fuzz/` — exactly `fuzz_json_parse.c`, `fuzz_b64.c`,
  `fuzz_zstd_decode.c`, `fuzz_path_safe.c`, `fuzz_repo_strip_v2.c`; no slip39
  harness.
- `grep -in keyshare recovery/docs/EXEMPTIONS.md recovery/docs/AUDIT_FINDINGS.md`
  → rc 1 (no mentions).
- `.github/workflows/audit-gate.yml:5-19` — push/PR paths list
  `recovery/src/lcsas-restore/**`, tests, fuzz, coverage_check.py,
  sanitize.sh, Makefile — not `recovery/src/lcsas-keyshare/**`.
- Partial existing coverage: `recovery/Makefile:84` — `$(BUILD)/test_keyshare`
  is in TESTS, so the 45 vectors run under `make -C recovery test`.
- `.claude/skills/key-escrow/PLAN.md:86` — C5.3 `[~]`: "Full
  coverage-c/EXEMPTIONS/fuzz integration of the new dir = documented
  follow-up."

## Fix design

1. **Coverage** — `recovery/Makefile` coverage-c: add a second filter,
   `--filter 'src/lcsas-keyshare/.*'` (gcovr accepts repeated `--filter`),
   keeping `--exclude '$(VENDOR)/.*'`. The keyshare objects are already built
   and linked by `test_keyshare` within the same `$(BUILD)`, so they pick up
   instrumentation for free; verify `coverage.json` now lists
   `slip39.c`/`wordlist.c`/`main.c` files and that the combined LINE_COVERAGE
   still clears THRESHOLD (wordlist.c is data — if it drags the number, add a
   gcovr `--exclude 'src/lcsas-keyshare/wordlist.c'` with a one-line
   justification in EXEMPTIONS.md). Extend `main.c` coverage by routing its
   parsing through the KEY-01 `lcsas_keyshare_extract` library function so the
   un-unit-testable remainder is thin.
2. **Fuzzing** — new `recovery/fuzz/fuzz_slip39_mnemonic.c`: split the fuzz
   input into ≤64 newline-delimited "mnemonics" (and, post-KEY-01, also feed
   the raw buffer through `lcsas_keyshare_extract`), call
   `lcsas_keyshare_recover_password` with a small passphrase matrix; assert no
   crash/leak/UB, never a success with `pwlen > LCSAS_KEYSHARE_MAX_PW`.
   Makefile targets `fuzz-keyshare` / `fuzz-keyshare-smoke` (60 s), added to
   the `fuzz-smoke` aggregate so `make -C recovery audit-gate` includes it.
   Seed corpus: the 45 official vectors + one full card file.
3. **Sanitize** — wire `test_keyshare` into the existing `make -C recovery
   sanitize` ASan/UBSan run (currently a manual one-off per PLAN C5.3).
4. **CI trigger** — `.github/workflows/audit-gate.yml`: add
   `recovery/src/lcsas-keyshare/**` to both push and pull_request path lists.
   (The broader filter holes — vendored sqlite/zstd, lcsas-iso9660,
   lcsas-init, scripts, the nonexistent sanitize.sh reference — are the GATE
   plan's scope; only the keyshare line lands here to avoid collision.)
5. **Ledger honesty** — add a keyshare section to
   `recovery/docs/EXEMPTIONS.md` stating what is and isn't covered (e.g.
   wordlist.c data exclusion), and mark PLAN.md C5.3 `[x]`.

No schema/catalog or shipped-binary behavior change (unless fuzzing finds
bugs — fix-forward and rebuild via `make -C recovery keyshare-arches`).

## Tests & gates

- `make -C recovery coverage-c` — keyshare sources appear in
  `build/coverage.txt`; gate threshold unchanged (88 documented; the CI-60
  drift is the GATE plan).
- `make -C recovery fuzz-keyshare-smoke` — 60 s clean run, clang+libFuzzer
  (opt-in locally, runs in audit-gate CI via `fuzz-smoke`).
- `make -C recovery sanitize` — includes test_keyshare; clean ASan/UBSan.
- CI: a do-nothing whitespace PR touching only
  `recovery/src/lcsas-keyshare/slip39.c` must trigger the audit-gate workflow
  (verify once via `gh run list` after merge).
- Coverage baseline pin: extend
  `tests/recovery_hardening/test_tier1_coverage_baseline.py` (opt-in
  `LCSAS_COVERAGE=1`) to assert keyshare files are present in the report —
  guards against the filter silently regressing.

## Acceptance criteria

- [ ] coverage-c report includes src/lcsas-keyshare files and still passes THRESHOLD.
- [ ] fuzz_slip39_mnemonic builds, smoke-runs 60 s clean, and is part of `fuzz-smoke`/audit-gate.
- [ ] sanitize target exercises test_keyshare.
- [ ] PRs touching only keyshare source trigger audit-gate in CI.
- [ ] EXEMPTIONS.md documents the keyshare dir; PLAN.md C5.3 closed.

## Dependencies & related plans

- **KEY-01 / KEY-07** add the parsing code this gate must watch — land this
  gate no later than those (ideally between KEY-01 and KEY-07).
- **GATE** "audit-gate workflow path filter has holes" — umbrella for the
  other path-filter fixes; coordinate to avoid double-editing the workflow.
- **GATE** "CI audit-gate runs THRESHOLD=60" — threshold value itself.

## Effort

1.5 days: 0.5 Makefile/coverage + sanitize, 0.75 fuzz harness + corpus, 0.25
CI + docs. Needs clang+libFuzzer and gcovr locally (already the audit-gate
toolchain; watch the known gcovr-version drift).

---
**Implemented:** 2026-06-13. As planned, with two scoping deviations made
explicit: (1) sanitize needed no change — `test_keyshare` is already in
`TEST_BINS`, so `make sanitize` builds+runs it under ASan/UBSan/LSan;
documented in EXEMPTIONS. (2) `wordlist.c` has zero gcov-executable lines
(pure const data) so it neither needs the planned `--exclude` nor drags
coverage; not excluded. coverage-c gained Step 3d to drive `main.c` via
real `lcsas key split` cards (success path) + usage/error invocations
(main.c 0% → 66.9%); EXEMPTIONS documents the keyshare dir narratively
(the FENCE line-contract stays lcsas-restore-scoped, as the plan noted).
Validated: `coverage-c` EXIT=0, LINE_COVERAGE=92.1%, slip39.c 90.7% /
main.c 66.9% in report, exemptions_check PASS; `fuzz-keyshare-smoke` 60s
0 crashes; lint+typecheck clean; audit-gate.yml YAML parses with the
keyshare path. CI trigger verified at next checkpoint push.
