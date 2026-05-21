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
| 85% (default) | Measured floor after Phase 8 (92.4% overall, all 16 files ≥ 85%). Prevents regressions. |
| 90% (next step) | Achievable once `repo.c` (85.9%) gets a compressed-pack-blob fixture covering the zstd-decode branch in `read_blob`. |
| 95% (aspirational) | Target after a fault-tolerant gcov runtime patch — current LD_PRELOAD shim works on non-coverage builds (verifies robustness; 0 prod crashes) but cannot accumulate .gcda data when fault-injecting the coverage build (gcov runtime crashes before flushing). |

**Why not 100%?** Three constraints:
1. Many `malloc`/`calloc`/`realloc` error branches require fault injection — the `make fault-inject` target (issue #165) covers some, but only branches that the test binaries actually reach.
2. `disc_locator.c` (currently 81.6%) has filesystem-dependent branches (chroot, mount-namespace prompts, fs-full handling) that require either user-namespace fixtures or `unshare(2)` setup the tests don't currently do.
3. `tree.c` and `repo.c` exercise restic-format encrypted data; their happy paths are covered by the blind-restore e2e but the local C unit tests use stub fixtures that fail at decryption. Bringing these to 95%+ needs a Python-side helper that produces valid encrypted blobs (master key, AES-CTR + Poly1305-AES tag + scrypt-derived KEK).

## Per-file coverage (2026-05-21, after Phase 8)

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
| main.c | 94.8% | **Phase 8**: real-fixture CLI tests (list-snapshots, verbose restore, snapshot find, --meta-disc, --pack-cache-dir, snapshot not found) |
| lcsas_io.c | 90.3% | |
| tree.c | 89.2% | Phase 7: encrypted-fixture walk/dir/symlink/unsafe-name/unsupported branches |
| disc_locator.c | 88.5% | **Phase 8**: catalog.db discovery, drain chunk-limit, cache_bytes_used walk, interactive prompt, mkdir_p failure |
| repo.c | 85.9% | Phase 7: encrypted key/index/snapshot + v2-zstd + supersedes branches |
| **Overall** | **92.4%** | Baseline: 78.5% (pre-Phase-1) → +13.9 pp |

## Phase 7: encrypted-fixture generator

`recovery/tests/fixtures/gen_fixture.py` produces a deterministic but valid restic-format repository (scrypt-derived KEK + AES-CTR + Poly1305 MAC) covering:

- Encrypted key file (decrypts with password `"test"`)
- v2-zstd-prefixed index file (exercises `lcsas_repo_strip_v2_prefix` zstd branch in `repo.c`)
- v1 index file with `supersedes` to a second index (exercises the dedup branch)
- Encrypted snapshot pointing at a root tree
- Encrypted pack containing: a data blob, a sub-tree blob, and the root tree blob
- Root tree with diverse node types: file, dir (with subtree → another file), symlink (safe), symlink with traversal target (rejected), node with `..` name (rejected), node with `/` in name (rejected), unsupported `chardev` type (skipped)

`test_repo.c` exercises the full API (`lcsas_repo_load_keys_dir`, `lcsas_repo_load_key_file`, `lcsas_repo_decrypt`, `lcsas_repo_load_index`, `lcsas_repo_load_snapshots`, `lcsas_repo_read_blob`, `lcsas_blob_index_find`, `lcsas_snapshot_latest`/`_find`, `lcsas_tree_restore`) against this fixture and asserts on the restored filesystem state.

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
