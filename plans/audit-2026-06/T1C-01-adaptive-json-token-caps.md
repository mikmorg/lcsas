# T1C-01: Adaptive JSON token buffers; fail loud on index parse overflow

**Priority:** P1 · **Severity:** high · **Dimension:** tier1-c · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: recovery/docs/AUDIT_FINDINGS.md:113 (tree.c fuzz-coverage gap only; the token-cap scaling cliff and the silent index-skip are untracked)
**Suggested GH issue title:** Make tier-1 JSON parsing size-adaptive; never skip an index silently

## Problem

The tier-1 C restorer (`lcsas-restore`) — the binary the heir is told to trust for 35 years — parses every
restic JSON structure into a FIXED-size token array and gives up the moment it fills. The caps are small
relative to real repositories: 16384 tokens for index pass-1, 32768 for pass-2 (~3,000 blob entries at
~10 tokens each), 65536 for tree blobs (~2,700 directory entries, or one large file's chunk list). Real
restic/rustic writers flush index files at ~50,000 blobs, a family-photos folder easily holds 3,000+ files,
and a single 60 GB disk image has tens of thousands of 1–8 MB chunks — all perfectly ordinary data that
tier-1 cannot restore today. Our own fixture generator (`gen_fixture.py`) documents the cliff and
deliberately splits its synthetic data to dodge it, so no existing test ever hits it.

The two failure modes diverge. A tree-blob overflow aborts the whole restore with the generic
`ERROR: tree restore failed` — no hint that the cause is a big folder/file or that tier-2 would succeed.
An index-file overflow is worse: the entire index file is **silently skipped** (`if (ntoks <= 0) { free(plain);
continue; }`), dropping every blob it described; the restore proceeds and later dies with a cryptic
`blob not in index` for seemingly random files. (Verifier correction: because `restore.sh` defaults
`LCSAS_TIER_FALLBACK=0`, the end state is always a loud non-zero abort, never an exit-0 partial restore —
but the abort is generic and undiagnosable, and the heir has no path forward.)

The petabyte stress test does not cover this: it creates 3,000 *undecryptable* index stubs (a BUG-3
dynamic-array regression test) and never feeds one valid dense index or one wide directory.

## Evidence

All re-checked against current code (2026-06-10):

- `recovery/src/lcsas-restore/repo.c:481-482` — `pass1_toks = calloc(16384, ...)`, `pass2_toks = calloc(32768, ...)`.
- `recovery/src/lcsas-restore/repo.c:520-522` (pass-1) and `repo.c:574-576` (pass-2) —
  `if (!plain) continue;` … `if (ntoks <= 0) { free(plain); continue; }` — a whole index file silently dropped on overflow.
- `recovery/src/lcsas-restore/repo.c:714-715` — same pattern in `lcsas_repo_load_snapshots` with a fixed
  `lcsas_json_tok toks[256]` (repo.c:703): a snapshot with a long `paths`/`tags` array silently vanishes from the list.
- `recovery/src/lcsas-restore/tree.c:781-785` — `malloc(sizeof(lcsas_json_tok) * 65536)`;
  `if (ntoks <= 0 || toks[0].type != LCSAS_JSON_OBJECT) goto out;` with `rc = -1`.
- `recovery/src/lcsas-restore/main.c:505` — `fprintf(stderr, "ERROR: tree restore failed\n");` (the only diagnostic).
- `recovery/src/lcsas-restore/json_q.c:27-28` — `alloc_tok` returns `-2` at the cap; `json_q.c:228-229`
  propagates it out of `lcsas_json_parse` (every `parse_*` does `if (idx < 0) return idx;`).
- `recovery/src/lcsas-restore/repo.c:434-450` — `parse_blob_entry` decodes id/offset/length/type/uncompressed_length: ~9–11 tokens per blob entry.
- `recovery/tests/fixtures/gen_fixture.py:984-987,1078-1079,1115-1118` — fixture generator self-documents
  "~3500 blob entries max per file" / "no more than ~3000 files" per subtree and splits to stay under (`ORPHANS_PER_FILE = 3000`).
- `tests/recovery_hardening/test_tier1_petabyte_fixture.py:1-19` — header confirms stubs are undecryptable; asserts crash-safety only.
- `recovery/scripts/restore.sh:311,941` — `LCSAS_TIER_FALLBACK` defaults to 0: tier-1 failure aborts the run.

## Fix design

**Core: a retrying, heap-growing parse wrapper.** Token count is bounded by input length (every token
consumes ≥1 source byte), so on a `-2` cap-hit we can re-parse with a bigger buffer instead of aborting.
Add to `json_q.c`/`json_q.h` (the tokenizer core stays allocation-free; this wrapper is the documented exception):

```c
/* Parse with a heap-grown token buffer.  Starts at initial_toks,
 * doubles on cap-hit (-2), bounded by min(len+1 tokens,
 * lcsas_json_max_tok_bytes).  On success *toks_out is malloc'd
 * (caller frees) and the token count is returned.
 * Returns -1 for malformed JSON, -2 if still over the ceiling. */
long lcsas_json_parse_alloc(const char *src, size_t len,
                            lcsas_json_tok **toks_out,
                            size_t initial_toks);

extern size_t lcsas_json_max_tok_bytes;  /* default 256 MiB; protects 32-bit armv7 */
```

`main.c` reads an optional `LCSAS_MAX_JSON_MIB` env override into `lcsas_json_max_tok_bytes`
(test seam + escape hatch; document it in the restore.sh env-var table at restore.sh:~305-315 so
`tests/recovery_hardening/test_env_var_docs.py` stays green).

**Call-site changes — every overflow becomes either success or a loud, named failure:**

1. `repo.c` `lcsas_repo_load_index` (both passes): replace the fixed `pass1_toks`/`pass2_toks` with
   `lcsas_json_parse_alloc(plain, plen, &toks, 16384)` per file. Replace the silent skip:
   ```c
   if (ntoks == -2) {
       fprintf(stderr, "ERROR: index file %s is too large for tier-1 to parse;\n"
                       "       use tier-2 (rustic) for this repository\n", path);
       free(plain); goto out;          /* rc = -1: load_index fails BEFORE any restore I/O */
   }
   if (ntoks <= 0) {
       fprintf(stderr, "ERROR: index file %s: invalid JSON after decrypt\n", path);
       free(plain); goto out;
   }
   ```
   For the `if (!plain) continue;` decrypt-failure skip: emit a per-file
   `WARNING: index file %s failed authentication; skipping` plus an end-of-load summary count
   (stays non-fatal for MAC failures so the petabyte BUG-3 regression premise holds; the *size-cap*
   decrypt reason becomes fatal — that split is T1C-05, implement together).
2. `tree.c:781-785`: replace the 65536 `malloc` with `lcsas_json_parse_alloc(blob, blob_len, &toks, 1024)`.
   On `-2`: `ERROR: tree blob %.64s is too large for tier-1 (a directory with very many entries, or a file
   with very many chunks); use tier-2 (rustic)`. On `-1`/non-object: `ERROR: tree blob %.64s: invalid JSON`.
   The 1024-token start also shrinks per-recursion-frame heap from ~2.6 MB to ~40 KB for typical trees
   (feeds T1C-04).
3. `repo.c` `lcsas_repo_load_snapshots` (~703-715): use `parse_alloc(..., 256)`; on `-2`/`-1`, loud
   per-file error and fail the load (a vanished snapshot is silent data loss for `--list`/selection).
4. `recovery/docs/FORMAT.txt`: add a "TIER-1 LIMITS" note: parsing is size-adaptive; the only ceiling is
   `LCSAS_MAX_JSON_MIB` (default 256 MiB of token memory) and the 256 MiB zstd cap (T1C-05).

No catalog/schema impact — read-side C only. Binaries on already-burned meta discs keep the old caps
forever; the fix ships on the next meta build (regenerate `recovery/bin/*` per the keyshare-arches
pattern, all 6 targets).

## Tests & gates

New hardening tests follow the `test_tier1_petabyte_fixture.py` pattern (integration-marked, real binary
from `recovery/build/` or `recovery/bin/x86_64/`, skip if absent):

- `tests/recovery_hardening/test_tier1_dense_index.py` — extend `gen_fixture.make_stress_fixture` with a
  `dense_index=True` flag that writes all orphan entries into ONE index file (bypass `ORPHANS_PER_FILE`);
  generate 6,000 entries (~60k tokens > 32768). Assert exit 0 and the real file restores byte-identical —
  and per the verdict's refinement, assert stderr contains no generic/cryptic failure (`tree restore failed`,
  `blob not in index`). This is the case the petabyte test sidesteps.
- `tests/recovery_hardening/test_tier1_wide_directory.py` — `make_stress_fixture(n_files=5000, n_subdirs=1)`
  (one subtree, >100k tokens). Assert all 5,000 files restored byte-identical.
- `tests/recovery_hardening/test_tier1_large_file.py` — extend gen_fixture to emit one file node whose
  `content` array repeats the data-blob id 40,000 times (valid restic semantics, tiny fixture). Assert the
  restored file equals payload × 40,000 (hash compare).
- `recovery/tests/test_json.c` — `lcsas_json_parse_alloc` growth case (array of 10k numbers, initial=16 →
  correct count); ceiling case (set `lcsas_json_max_tok_bytes` tiny → returns `-2`, not `-1`).
- `recovery/tests/test_repo.c` — with `lcsas_json_max_tok_bytes` clamped small, `lcsas_repo_load_index`
  on the standard fixture returns <0 (fails loud) instead of returning success with an empty index.
- The tree fuzz harness (`fuzz_tree_restore.c`) is specified in T1C-04; it exercises the new tree.c parse path.

Gates: C unit tests run in `make -C recovery test` → `audit-gate` steps 1/3 (always-on in
`.github/workflows/audit-gate.yml` for recovery-path changes). The pytest hardening tests run under
`make test-recovery-hardening` — local-only until GATE "shippable-build gate in CI" lands; note that
dependency below.

## Acceptance criteria

- [ ] One valid index file with 6,000 blob entries: restore exits 0, byte-identical output.
- [ ] One directory with 5,000 entries: full restore, byte-identical.
- [ ] One file with 40,000 content chunks: full restore, byte-identical.
- [ ] With `LCSAS_MAX_JSON_MIB=1` and a fixture exceeding it: exit non-zero, stderr names the index file /
      tree blob and says "use tier-2 (rustic)"; never a bare `tree restore failed` or `blob not in index`.
- [ ] No code path in `lcsas_repo_load_index`/`load_snapshots` skips a file without a printed reason.
- [ ] `make -C recovery audit-gate THRESHOLD=60` passes (sanitize + fuzz-smoke included).
- [ ] gen_fixture.py token-budget warnings (lines 984-987, 1078-1079, 1115-1118) removed or rewritten to
      describe the adaptive behavior.

## Dependencies & related plans

- **T1C-05** (256 MiB decompress cap fail-loud) — same `load_index` surgery; implement in the same PR.
- **T1C-04** (tree recursion depth cap + fuzz) — depends on this plan's small-start `parse_alloc` for its
  per-frame memory budget; land T1C-01 first.
- **GATE** "the 'shippable build' gate (tests/recovery_hardening) never runs in CI" — until that lands,
  the new pytest tests gate merges only locally.
- After merge: rebuild and re-commit `recovery/bin/*` tier-1 binaries (see GATE "binary staleness" plan).

## Effort

3 days: 1.5 impl (parse_alloc + three call sites + messages + env knob), 1.5 fixtures/tests
(gen_fixture extensions are the bulk). Needs only the local Linux toolchain (clang for sanitize/fuzz);
qemu re-verification of cross-built bins per the usual recovery build flow.

---
**Implemented:** 2026-06-13. As planned: added `lcsas_json_parse_alloc` (heap-grown, calloc'd, len+1 / `lcsas_json_max_tok_bytes` ceiling) + `LCSAS_MAX_JSON_MIB` env knob in main.c; rewired both index passes, `load_snapshots`, and `tree.c` to fail loud (named file + "use tier-2") on `-2`/`-1` instead of silently skipping; MAC-failure index skips now warn + tally (BUG-3 non-fatal premise preserved). gen_fixture gained `--dense-index` + `--chunks-per-file`; three new hardening tests (dense index 6k, wide dir 5k, large file 40k chunks) + clamped-ceiling fail-loud test, plus C tests in test_json.c/test_repo.c. Sanitize gate clean (0 ASan/UBSan/LSan); fuzz-json-smoke 0 crashes. Rebuilt + recommitted all 5 git-tracked tier-1 `lcsas-restore` bins (x86_64/aarch64/armv7 musl-static via zig, x86_64-windows PE, aarch64-macos Mach-O); x86_64-macos restore is gitignored/untracked upstream so left as-is (pre-existing inconsistency). T1C-05 (decrypt cap) deferred — separate plan despite the "same PR" note.
