# Tier-1 C Binary Audit Gate

`make audit-gate` is an opt-in comprehensive quality gate for
`recovery/src/lcsas-restore/`.  It is **not** wired into the default
`make gate` — run it explicitly before merging any PR that touches the
tier-1 C binary.

## Quick start

```bash
# From repo root (default threshold 60%):
make audit-gate

# With aspirational threshold:
make audit-gate THRESHOLD=95

# From recovery/ directly:
make -C recovery audit-gate
make -C recovery audit-gate THRESHOLD=95
```

## What it does (4 steps, ~10 min)

| Step | Target | Description |
|------|--------|-------------|
| 1 | `coverage-c` | Rebuilds with `--coverage`, runs C unit tests + tier-1 Python suite, generates gcovr HTML + JSON report |
| 2 | `sanitize` | Rebuilds with `clang -fsanitize=address,undefined,leak`, runs full test suite.  0 findings = pass |
| 3 | `fuzz-smoke` | Runs all 5 LibFuzzer harnesses for 60 s each.  0 crashes = pass |
| 4 | `coverage_check` | Per-file threshold check against the gcovr JSON report |

## Coverage thresholds

| Threshold | Meaning |
|-----------|---------|
| 70% (default) | Measured floor after Phase 5 (87.1% overall). Prevents regressions — no file should drop below this. |
| 85% (next step) | Achievable once `tree.c` and `repo.c` get fixture-based tests with encrypted blobs (currently 70.3% and 75.9% — see below). |
| 95% (aspirational) | Target after full fixture work. Requires Python-generated valid restic key/index/pack fixtures, OR cheaper expansion of `disc_locator` and `repo` C unit tests. |

**Why not 100%?** Three constraints:
1. Many `malloc`/`calloc`/`realloc` error branches require fault injection — the `make fault-inject` target (issue #165) covers some, but only branches that the test binaries actually reach.
2. `disc_locator.c` (currently 81.6%) has filesystem-dependent branches (chroot, mount-namespace prompts, fs-full handling) that require either user-namespace fixtures or `unshare(2)` setup the tests don't currently do.
3. `tree.c` and `repo.c` exercise restic-format encrypted data; their happy paths are covered by the blind-restore e2e but the local C unit tests use stub fixtures that fail at decryption. Bringing these to 95%+ needs a Python-side helper that produces valid encrypted blobs (master key, AES-CTR + Poly1305-AES tag + scrypt-derived KEK).

## Per-file coverage (2026-05-21, after Phase 5)

| File | Coverage | Notes |
|------|----------|-------|
| aes.c | 100% | Crypto primitive — covered by `test_aes` |
| hex.c | 100% | |
| sha256.c | 100% | |
| zstd_dec.c | 100% | Both probe paths covered (Phase 1) |
| path.c | 98.3% | |
| scrypt.c | 98.0% | |
| catalog.c | 98.0% | Boosted Phase 5 |
| json_q.c | 97.1% | All escape paths + literals covered (Phase 5) |
| b64.c | 95.7% | |
| poly1305.c | 95.0% | |
| pbkdf2.c | 94.7% | |
| lcsas_io.c | 90.3% | |
| main.c | 88.1% | CLI arg coverage (Phase 5) |
| disc_locator.c | 81.6% | Drain/scan paths now covered (Phase 5) |
| repo.c | 75.9% | Needs fixture: valid restic keys + indexes |
| tree.c | 70.3% | Needs fixture: valid encrypted tree blobs |
| **Overall** | **87.1%** | Baseline: 78.5% (pre-Phase-1) |

## Fault injection (`make fault-inject`)

Issue #165's malloc fault-injection harness — `recovery/scripts/malloc_inject.c` —
is an `LD_PRELOAD` shim that fails the Nth allocation.  The driver
(`recovery/scripts/run_fault_inject.py`) sweeps N=1..total across every
test binary and hard-fails on any SIGSEGV/SIGABRT/SIGBUS/SIGFPE or timeout.

A graceful error return (any non-crash exit code) is fine — what we're
catching is unhandled malloc failures in production code that would
crash under genuine OOM or hostile allocator behavior.

```bash
make -C recovery fault-inject            # full sweep (~3 min)
make -C recovery fault-inject MAX_N=100  # smoke sweep (~5 s)
```

When run after `make coverage-c` builds the binaries with `--coverage`,
the sweep accumulates `.gcda` data on every error branch it triggers,
boosting per-file coverage on `repo.c` / `catalog.c` /  `disc_locator.c`
by exercising the unreachable-by-default malloc-failure goto-out paths.

The hardening test `tests/recovery_hardening/test_tier1_fault_inject.py`
pins zero-crashes as a regression gate (opt-in via `LCSAS_FAULT_INJECT=1`).

## Known exclusions

(arena.c was removed in PR #175 — no longer a coverage exception.)

## Interpreting failures

### Coverage below threshold

```
src/lcsas-restore/disc_locator.c    60.3%  FAIL (<75%)
```

Open `recovery/build/coverage/index.html` in a browser and navigate to
the file.  Red lines are unexercised.  Common causes:
- Disc-not-found code paths (require a mounted ISO to trigger)
- Error-handling paths after `malloc` failure (issue #165)
- Platform-specific branches (Windows path separator handling)

### ASan / UBSan / LSan finding

The `sanitize` step will print a stack trace and the make target will
fail.  Check `recovery/docs/SANITIZER_SUPPRESSIONS.txt` — if the
finding is in vendored sqlite3 or zstd, a suppression may be
appropriate (with a code comment explaining why).

### Fuzz crash

```
[fuzz-path-smoke] crash found: recovery/build/fuzz/crash_<hash>
```

Reproduce with:
```bash
recovery/build/fuzz/fuzz_path_safe recovery/build/fuzz/crash_<hash>
```

Add the crash input to `recovery/fuzz/corpus/path_safe/` (named
descriptively), fix the bug, and re-run `fuzz-smoke`.

## Adding a new harness

1. Create `recovery/fuzz/fuzz_<name>.c` with a `LLVMFuzzerTestOneInput`
   entry point.
2. Add a seed corpus in `recovery/fuzz/corpus/<name>/`.
3. Add build, `fuzz-<name>-smoke`, and `fuzz-<name>` targets to
   `recovery/Makefile` following the pattern of the existing harnesses.
4. Add `fuzz-<name>-smoke` to the `fuzz-smoke` dependency list.
5. Add the new targets to `.PHONY`.

## CI integration

See `.github/workflows/audit-gate.yml` (issue #163).  The workflow
triggers on pushes to paths matching `recovery/src/lcsas-restore/**`,
`recovery/tests/**`, `recovery/fuzz/**`, and `recovery/Makefile`.

It runs `make -C recovery audit-gate` with the default threshold.
