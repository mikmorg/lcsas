# Tier-1 C Binary Audit Gate

`make audit-gate` is an opt-in comprehensive quality gate for
`recovery/src/lcsas-restore/`.  It is **not** wired into the default
`make gate` — run it explicitly before merging any PR that touches the
tier-1 C binary.

> **Status reconciliation (audit remediation, 2026-06).**  This doc
> describes the *mechanism* of the audit gate.  The point-in-time audit
> *findings* and the deferred-work parking lot have been consolidated
> into [`STATUS_LEDGER.md`](STATUS_LEDGER.md) — start there for what is
> open vs resolved.  The line-by-line coverage contract remains in
> [`EXEMPTIONS.md`](EXEMPTIONS.md) (it is enforced live by
> `exemptions_check.py`, not a static ledger).  Landed since the original
> audit: fault injection (`make fault-inject`, #165), the
> `fuzz_tree_restore` harness (T1C-04), and the FMA-09 fs-full drain
> seam are all in-tree and gated.

## Quick start

```bash
# From repo root (default threshold 88% — the measured floor):
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
| 3 | `fuzz-smoke` | Runs all 8 LibFuzzer harnesses (json, b64, zstd, path, repo, keyshare, tree, rs03) for 60 s each.  0 crashes = pass |
| 4 | `coverage_check` | Per-file threshold check against the gcovr JSON report |

## Coverage thresholds

| Threshold | Meaning |
|-----------|---------|
| 88% (default) | Measured floor after Phase 9 (93.9% overall, all 16 files ≥ 88%). Prevents regressions. |
| 95% (achieved by 10 / 16 files at the 2026-05-21 snapshot) | aes.c, hex.c, sha256.c, zstd_dec.c, path.c, scrypt.c, catalog.c, json_q.c, b64.c, poly1305.c. |
| 95% (aspirational for the rest, tree.c aside) | repo.c (90.6%), disc_locator.c (88.5%), lcsas_io.c (90.3%), pbkdf2.c (94.7%), main.c (94.8%) — the malloc-failure error branches and contrived-corruption paths need either a fault-tolerant gcov runtime patch or large amounts of fixture engineering for diminishing returns. |
| 90% (achievable ceiling for tree.c) | tree.c has ~35 lines of INTRACTABLE code: apply_node_ownership (requires root for geteuid()!=0 guard), ENOSPC classifiers (require filesystem-full target), FAT32 symlink error paths (require non-POSIX mount). These count against the denominator permanently. |

**Why not 100%?** Three constraints:
1. Many `malloc`/`calloc`/`realloc` error branches require fault injection — the `make fault-inject` target (issue #165) covers some, but only branches that the test binaries actually reach.
2. `disc_locator.c` (88.5%) has filesystem-dependent branches (chroot, mount-namespace prompts, fs-full handling) that require either user-namespace fixtures or `unshare(2)` setup the tests don't currently do.  The fs-full drain branches are now reachable via the `LCSAS_TEST_FULL_FS_DIR` seam exercised by `test_restore_space_preflight.py` (FMA-09); the residual lines are line-pinned VOLATILE/DEFERRED in `EXEMPTIONS.md`.
3. `tree.c` has ~35 INTRACTABLE lines (geteuid()!=0 chown guard, ENOSPC classifiers, FAT32/non-POSIX symlink paths) that cannot be reached in the standard coverage-c harness. The aspirational ceiling for tree.c is ~90%, not 95%.

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
| tree.c | **~90% (achievable ceiling)** | **Issue #269**: xattr+hardlink fixture nodes added; apply_node_xattrs (330-397) and hardlink success branch (541-563) now covered. ~35 INTRACTABLE lines (geteuid()!=0 chown, ENOSPC classifiers, FAT32 symlink paths) prevent reaching 95%. |
| main.c | 94.8% | Phase 8: real-fixture CLI tests |
| repo.c | **90.6%** | **Phase 9**: zstd-compressed data blob (uncompressed_length both with + without) + corrupted-zstd index + multiple keys/snapshots forcing sort |
| lcsas_io.c | 90.3% | |
| disc_locator.c | 88.5% | Phase 8: catalog.db discovery, drain chunk-limit, cache_bytes_used walk, interactive prompt, mkdir_p failure |
| **Overall** | **93.9%** | Baseline: 78.5% (pre-Phase-1) → +15.4 pp |

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

## Petabyte benchmark (`scaling_bench.py` + `LCSAS_PETABYTE=1`)

Phase 10 added two artefacts for characterising and stress-testing
production-code behaviour at petabyte-class blob index sizes:

```bash
# Scaling sweep across N=100, 1k, 10k, 100k, 1M blob index entries.
# Writes recovery/build/scaling.md with load/RSS/find_ns table.  ~6 min.
python3 recovery/scripts/scaling_bench.py

# End-to-end stress: 1M orphan blob entries + 1k restored files.
# Asserts RSS < 1.5 GiB and wall-clock < 10 min.  Opt-in.  ~5 min.
LCSAS_PETABYTE=1 pytest tests/recovery_hardening/test_tier1_petabyte_fixture.py \
    -v -m integration
```

Neither is wired into `make gate` — both are explicit performance
characterisation, not regression gates.  Use them when reasoning about
how `lcsas-restore` will behave on real-world archive sizes.

**Measured on this host (vm-desk1, 7.8 GiB RAM, 8 cores):**

| N entries | RSS (MiB) | per-find (ns) | per-find at 100× scale |
|----------:|----------:|--------------:|------------------------|
| 100       | 18        | 11,333        | ~1ms                   |
| 1k        | 18        | 76,894        | ~770 µs                |
| 10k       | 18        | 572,238       | ~5.7 ms                |
| 100k      | 18        | 6,022,275     | ~60 ms                 |
| 1M        | 111       | 61,156,104    | ~600 ms                |

At that (pre-Phase-11) measurement `find_ns_mean` grew linearly with N
— `lcsas_blob_index_find` was O(n).  **Phase 11 replaced the linear
scan with a sorted array + `bsearch`** (~28,000× faster at N=1M);
petabyte-scale restore is no longer find-bound.  The table above is
kept as the historical motivation — see `STATUS_LEDGER.md`
(Scalability) for the current numbers.

The `LCSAS_STRESS_LOOKUPS=N` env var on the binary is what
`scaling_bench.py` uses: it times N random `lcsas_blob_index_find` calls
after `lcsas_repo_load_index` returns, prints a single `[bench]` line
on stderr, and exits.  Production paths are unaffected.

## Differential oracle (`LCSAS_DIFF=1`)

Phase 14 added a tier-1 ↔ tier-2 byte-identical comparison.  For each
content profile (small_file, many_small_files, one_large_file,
deep_tree, unicode_names, symlinks_and_modes, empty_dir,
large_dir_node, setuid_modes) the test:

1. Builds a real restic-format repo via `rustic init` + `rustic backup`
2. Restores via `lcsas-restore`  (tier 1) into `tier1_out/`
3. Restores via `rustic restore` (tier 2) into `tier2_out/`
4. Asserts byte-identical trees: same content, same mode, same
   symlink targets, same presence

The `small_file` profile runs in the default integration suite as a
smoke test.  The remaining 7 profiles are opt-in:

```bash
LCSAS_DIFF=1 pytest tests/recovery_hardening/test_tier1_vs_tier2_differential.py -v
```

**Security note** (PR #197, issue #187): as of this PR, tier-1
honours absolute-target symlinks in restored snapshots (matching
tier-2 / rustic).  Previously tier-1 rejected them as a containment
defence.  Operators who relied on the implicit containment property
must now sandbox the restore target separately — see issue #187 for
the full rationale.  Relative-target symlinks are still validated
via `lex_resolve_inside` to ensure they resolve under the restore
root.

Phase 14 surfaced two real bugs on first run:
- `lcsas_create_file` hardcoded mode 0600 → fixed to honour the
  tree node's "mode" field via `fchmod` after `open()`.
- `lcsas_mkdir_p` hardcoded 0700 for all components → fixed to
  `chmod()` the leaf directory to the tree node's "mode" field.

Both were silent permission-downgrades on restore.  See PR thread
for details.

### Phase 14.1 — setuid/setgid/sticky parity (issue #195 → #201)

The `setuid_modes` profile (issue #195) backs up files with mode
`0o4755` (setuid), `0o2755` (setgid), and a directory with `0o1777`
(sticky) and asserts tier-1 ↔ tier-2 parity on bits beyond
`0o0777`.  Result: **divergence** — tier-1 strips all three bits
silently, tier-2 (rustic) preserves them.

Root cause: rustic stores `mode` in tree-node JSON as Go's
`os.FileMode` bit-field, **not** the POSIX 12-bit mode word.
Setuid lives at Go bit 23, setgid at bit 22, sticky at bit 20.
Tier-1's `& 07777` mask in `lcsas_create_file` (`lcsas_io.c:104`)
and `lcsas_tree_restore`'s dir `chmod` (`tree.c:350`) extract only
the POSIX-aligned bits, silently dropping the Go-encoded ones.

The `setuid_modes` profile is marked `xfail(strict=True)` against
issue #201.  When tier-1 learns the Go encoding the test becomes
XPASS-strict and forces the xfail marker to be removed alongside
the fix.

### Phase 14.2 — filename normalization parity (issue #195)

Tier-1's `lcsas_path_safe_name` (`path.c`) rejects names with a
leading `/`, empty segments (`//`), and `..` segments.  These
rules are unit-pinned in `recovery/tests/test_path.c` (`dotdot`,
`dotdot-start`, `dotdot-passwd`, `absolute`, `abs-bare`,
`trailing-slash`, `dotdot-trail`, `empty`).  The `gen_fixture.py`
restic-format fixture used by `test_repo.c` already injects
malicious node names (`"../escape"`, `"foo/bar"`, evil symlink
`"../../../etc/passwd"`) and the test asserts none of them
materialise on the restored tree.

Tier-2 (rustic) sanitizes input during `rustic backup` — a real
malicious tree cannot be created via the documented interface
without synthesising a tree blob directly.  Operator-visible
behaviour on a hostile snapshot therefore differs only in the
delivery channel (tier-1 logs `skip unsafe name: <name>` to stderr
and continues; rustic does not get the chance to fail on a name
its own backup pipeline never produced).  This is parity in
practice: both tiers refuse to materialise a `..`-escape or
absolute path under the restore target.

## Shell-level coverage (`make shell-coverage`)

Issue #213 added `bash -x`-based line coverage for
`recovery/scripts/restore.sh` (894 lines / ~393 executable at the
time; the script has since grown to ~1900 lines — `make
shell-coverage` output is the authoritative current count).
Pipeline:

1. `restore.sh` preamble (~line 30) honours `LCSAS_SHELL_TRACE=<path>`
   by enabling `BASH_XTRACEFD` + `set -x`.  No-op on dash/POSIX-sh.
2. `tests/recovery_hardening/conftest.py` has an autouse fixture
   that, when `LCSAS_TRACE_VIA_BASH=1`, rewrites every `['sh',
   restore.sh, ...]` subprocess invocation to use bash + propagates
   the trace env-var.
3. `tools/cov_shell.py` parses the resulting `+ <LINENO> <command>`
   trace lines and cross-references against the script's
   executable-line set (excludes blank/comment/structural lines
   and heredoc bodies).
4. `make shell-coverage` chains all of the above and gates at 60%.

**Baseline at #213: 61.1% (240/393 lines)**; the gate holds at 60%
as the script grows.  Tier-2 / tier-3 fallback branches dominated
the original uncov set — the #214 adversarial blind-restore variants
were the natural way to push that higher.

```bash
make shell-coverage
```

## Known exclusions

(arena.c was removed in PR #175 — no longer a coverage exception.)

## Documented coverage exemptions

Lines not covered by `make coverage-c` are individually justified in
[`EXEMPTIONS.md`](EXEMPTIONS.md).  Overall coverage sits at ~94%
(93.9% at the 2026-06 reconciliation — `STATUS_LEDGER.md` is the
authoritative tracker); the uncovered remainder is mapped to specific
intractable categories (EINTR retry, malloc fault injection against
gcov-instrumented code, defensive 4 KiB+ path overflow checks, etc.).

Every uncov line has either:
- a TRACTABLE entry in `EXEMPTIONS.md` slated for a future PR, or
- an INTRACTABLE entry with rationale, or
- a DEFENSIVE entry for safe-guard code that is provably unreachable
  given upstream invariants but kept for readability.

## Interpreting failures

### Coverage below threshold

```
src/lcsas-restore/disc_locator.c    86.1%  FAIL (<88%)
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

See `.github/workflows/audit-gate.yml` (issue #163; required-check
shape per #412/#414).  `push` triggers on paths matching
`recovery/src/**`, `recovery/tests/**`, `recovery/fuzz/**`,
`recovery/vendored/**`, `recovery/scripts/**`, `recovery/Makefile`,
and `recovery/MANIFEST.sha256`; `pull_request` is deliberately
UNFILTERED (path relevance moves into the workflow's `changes` job so
`audit-gate-required` can be a required status check).

It runs `make -C recovery audit-gate THRESHOLD=83` — the local 88% floor
(`recovery/Makefile` `THRESHOLD ?= 88`) minus the measured ~5 pt CI
environment delta (the conftest/lcsas-install effect; see the workflow's
Install step comment). The parity is pinned by
`tests/recovery_hardening/test_audit_gate_threshold_parity.py`.
