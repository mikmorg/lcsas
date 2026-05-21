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
| 60% (default) | Measured baseline floor (Phase 1 result).  Prevents regressions — no file should drop below this. |
| 95% (aspirational) | Target after petabyte-fixture work (issue #162) and fault-injection harness (issue #165).  `make audit-gate THRESHOLD=95` shows remaining work. |

**Why not 100%?**  Every `malloc`/`calloc`/`realloc` call has an error
branch that returns `-1` or `goto out`.  These are unreachable without
a fault-injection shim (see issue #165, deferred Phase 6).  100% would
require LD_PRELOAD tricks; 95% is the achievable ceiling.

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
