# GATE-03: macOS tier-1 binaries are built but never executed anywhere

> **STATUS: RESOLVED** — landed in `2531a86` (ci+tests: execute Mach-O tier-1 binaries on macOS runners [GATE-03]); guarded by `tests/recovery_hardening/test_tier1_macos_native.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** untracked (recovery/docs/UX_CONCERNS.txt ID 004 tracks Gatekeeper refusal — a different problem)
**Suggested GH issue title:** Add macOS CI runners that execute both Mach-O tier-1 binaries

## Problem

Cross-arch *execution* verification exists for three of the five cross
targets: Linux aarch64/armv7 run under qemu-user and Windows under wine
(local-only, and CI-runnable after GATE-02 installs qemu/wine). For the two
macOS targets there is no execution path at all: no macOS runner, no test that
ever runs `recovery/bin/{aarch64-macos,x86_64-macos}/lcsas-restore` or
`lcsas-keyshare`. Every "macos" reference in the test tree is a presence check
(meta bundling) or a dispatcher string match (platform detect). Both binaries
are built with `zig cc -target <arch>-macos` against zig's bundled libSystem
stubs — precisely the kind of build that can produce a Mach-O the loader
rejects, or that links a symbol the stub has but real libSystem gates
differently.

A zig regression (wrong `-target`, broken stub linkage, Mach-O
incompatibility with a newer macOS) ships green through every gate including
the blind restore, which runs on Linux/cdemu. For an heir restoring on a Mac —
arguably the most likely consumer platform decades out — tier 1 is an entirely
unproven artifact. The bar is low: the binaries are committed (after GATE-01),
the encrypted test fixture is committed, and GitHub hosts both Intel and ARM
macOS runners; a list-snapshots + full restore takes seconds.

## Evidence

Re-checked 2026-06-10 against master:

- `recovery/Makefile:377-396` — `bin/x86_64-macos/lcsas-restore` /
  `bin/aarch64-macos/lcsas-restore` rules build and `cp` only; `macos:`
  aggregate target builds, never runs.
- Grep for `macos|darwin` across `tests/` and `recovery/tests/`: only
  `test_meta_bundling_completeness.py` (presence),
  `test_restore_platform_detect.py` (dispatcher strings), SUPPORTED_ARCHES
  asserts, and one `cross_build` (build-not-run) in
  `test_recovery_orchestration.py`.
- `.github/workflows/` — `test.yml` and `audit-gate.yml`, both
  `runs-on: ubuntu-latest`.
- Contrast: `tests/recovery_hardening/test_tier1_aarch64_qemu.py:40-58` and
  `test_tier1_windows_wine.py:43-66` exist for the other arches (skipif on
  binary+emulator presence, default to the *committed* `recovery/bin/...`
  artifacts).
- Fixture: `recovery/tests/fixtures/repo` (password `"test"`, per
  `tests/recovery_hardening/test_tier1_unit.py:576-578`); keyshare vectors at
  `tests/fixtures/keyshare/vectors.json`.

## Fix design

**1. New `tests/recovery_hardening/test_tier1_macos_native.py`** — mirror the
qemu/wine harness pattern so the same checks run in CI *and* on any future
local Mac:

- `pytestmark = pytest.mark.skipif(sys.platform != "darwin", ...)`.
- Binary selection: `platform.machine()` → `arm64` ⇒
  `recovery/bin/aarch64-macos/`, `x86_64` ⇒ `recovery/bin/x86_64-macos/`;
  honour `LCSAS_RESTORE_BIN` override like the qemu harness.
- Tests (reuse helpers/assertions from `test_tier1_unit.py`'s real-fixture
  section):
  - `test_list_snapshots` — `--repo recovery/tests/fixtures/repo
    --password-file <pw=test> --list-snapshots` exits 0, stdout contains
    `/test` and `2026-05-21`.
  - `test_full_restore_byte_identical` — restore to tmpdir, sha256-compare
    restored files against the fixture's expected manifest.
  - `test_wrong_password_fails_cleanly` — nonzero exit, no crash signal.
  - `test_keyshare_combines_official_vector` — write a passing vector's
    mnemonics from `tests/fixtures/keyshare/vectors.json` to files, run
    `recovery/bin/<arch>-macos/lcsas-keyshare [--passphrase ...] f1 f2`,
    assert expected secret on stdout. (No Python test drives the C combiner
    binary anywhere today — grep confirms — so this is first execution
    coverage for the Mach-O keyshare too; KEY-06 owns its deeper gates.)

**2. New `.github/workflows/macos-tier1.yml`** — on push/PR touching
`recovery/src/**`, `recovery/vendored/**`, `recovery/bin/**`,
`recovery/Makefile`, plus `workflow_dispatch` and a weekly `schedule` (so
runner-image drift is caught even in quiet periods):

```yaml
jobs:
  tier1:
    strategy:
      matrix:
        include:
          - { runner: macos-15-intel, arch: x86_64 }   # Intel leg; macos-13 was
          - { runner: macos-14,       arch: arm64  }   # retired late 2025
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: actions/checkout@v4
      - run: xattr -rc recovery/bin || true   # belt-and-braces vs quarantine
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: pytest tests/recovery_hardening/test_tier1_macos_native.py -v
```

Per the verifier's correction: use `macos-15-intel` for the x86_64 leg (GitHub
retired `macos-13`; `macos-15-intel` is the supported Intel label through
~Aug 2027 — when *it* retires, the Intel leg should fall back to Rosetta 2 on
an arm64 runner: `arch -x86_64 recovery/bin/x86_64-macos/lcsas-restore ...`,
which still proves the Mach-O loads and runs). ~3 min per leg.

**3. Gatekeeper note (UX_CONCERNS ID 004 stays open):** binaries from a git
checkout carry no quarantine xattr, so CI execution does not prove an heir's
Finder double-click works — only that the binary itself is sound. Don't close
ID 004 on the back of this plan.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_tier1_macos_native.py` — always-on where
  `sys.platform == "darwin"`, honest skip elsewhere (so `make
  test-recovery-hardening` on Linux is unaffected).
- `.github/workflows/macos-tier1.yml` — always-on for recovery paths +
  weekly schedule + manual dispatch. Both matrix legs must pass.
- Negative check during development: point `LCSAS_RESTORE_BIN` at a Linux ELF
  on the Mac runner once and confirm the harness fails loudly (validates the
  gate can actually catch a mis-built artifact).

## Acceptance criteria

- [ ] Both matrix legs green: committed `aarch64-macos` and `x86_64-macos`
      `lcsas-restore` each list snapshots and restore the fixture
      byte-identically on a real macOS runner.
- [ ] Mach-O `lcsas-keyshare` reconstructs an official SLIP-0039 vector on
      both legs.
- [ ] A deliberately corrupted binary (truncate 1 KB on a scratch branch)
      turns the workflow red.
- [ ] Workflow triggers on `recovery/bin/**` changes (so binary regeneration
      PRs — GATE-08 — get execution proof automatically).

## Dependencies & related plans

- **GATE-01** (commit Intel-Mac binary) — hard prerequisite: the x86_64 leg
  has nothing to execute on a fresh checkout until it lands.
- **GATE-08** (binary staleness gate) — complementary: GATE-08 proves bins
  match source; this proves they run.
- **KEY-06** (keyshare outside audit gates) — owns coverage/fuzz for the
  combiner; this plan only adds Mach-O execution.
- **UX-08** (cross-OS journey gates) / **INFRA-01** — the full macOS *journey*
  (restore.sh on macOS, hdiutil mounts) builds on this binary-level gate.

## Effort

1.5 days: 0.5 the native harness, 1.0 workflow iteration on hosted Mac
runners (expect a few red runs for runner-image quirks). No local Mac needed.

---
**Implemented:** 2026-06-13. Added `tests/recovery_hardening/test_tier1_macos_native.py`
(darwin-only skipif; honest skip on Linux, verified) and `.github/workflows/macos-tier1.yml`
(macos-15-intel + macos-14 matrix, recovery-path + weekly + dispatch triggers).
Deviation: the keyshare case combines a real `lcsas key split` 2-of-5 card set
byte-exact rather than an official SLIP-0039 vector — the committed CLI binary
recovers an LCSAS-framed password (chains SLIP-0039 + the length-prefixed codec,
slip39.h `lcsas_keyshare_decode_master_secret`), so the raw-master-secret vectors
in vectors.json cannot be fed to it directly; verified all 15 valid vectors fail
the CLI's decode step on the host x86_64 binary. Test bodies validated green on
Linux against the x86_64 tier-1 binaries via `LCSAS_RESTORE_BIN`; truncated-binary
negative check confirms the gate goes red. No C/binary changes (no rebuild needed).
