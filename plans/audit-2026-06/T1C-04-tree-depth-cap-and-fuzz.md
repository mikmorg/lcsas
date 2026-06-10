# T1C-04: Depth-cap the tree walk; fuzz lcsas_tree_restore

**Priority:** P1 · **Severity:** medium · **Dimension:** tier1-c · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: recovery/docs/AUDIT_FINDINGS.md:113 (tree.c coverage-gap row mentions the missing fuzz target; the SIGSEGV-on-deep-tree behaviour has no action item)
**Suggested GH issue title:** Bound tree-restore recursion depth; add fuzz_tree_restore harness

## Problem

The tier-1 C restorer walks directory trees by recursing on the C stack, one
`tree_restore_recurse` frame per directory level, with no depth bound. Each frame holds
~5.3 KB of fixed buffers (`name_buf[1024]` + `type_buf[32]` + `node_path[4096]`) plus a
heap-live token buffer (65536 × 40 B ≈ 2.6 MB today) and the decrypted blob, both freed
only *after* all children return — so memory is O(depth × ~2.6 MB) heap and ~6 KB/level
stack. Under the default 8 MB stack ulimit, a tree ~1,300+ levels deep kills the process
with SIGSEGV and **zero diagnostic**. The file header openly admits this and prescribes
`ulimit -s unlimited` — a workaround `restore.sh` never applies (no `ulimit` anywhere in
the script) and a non-technical heir will never discover. To the heir, a SIGSEGV is
indistinguishable from "the recovery software is broken", which forfeits trust in the one
binary the whole 35-year design asks them to trust.

Separately, the recursive node-walk — the most state-rich parser in the binary, handling
attacker-shaped JSON after decryption — has **no fuzz coverage at all**. `recovery/fuzz/`
contains harnesses for b64, json_parse, path_safe, repo_strip_v2, and zstd_decode, but
nothing drives `lcsas_tree_restore` over crafted tree blobs; AUDIT_FINDINGS.md confirms
the remaining uncovered tree.c lines are exactly these corrupted-blob paths. The only
depth coverage anywhere is the differential test's `deep_tree` profile — 6 levels.

Triggering the crash requires a pathological-but-authenticated tree (repo key held, or a
legitimately extreme layout), hence medium — but the failure mode (signal death, no
message) is the worst possible UX for the durable path, and the fuzz gap leaves the
walker's malformed-input behaviour unverified.

## Evidence

Re-checked 2026-06-10:

- `recovery/src/lcsas-restore/tree.c:1-11` — header: "for very deep trees this could
  overflow … For pathological depths use `ulimit -s unlimited` before invoking."
- `tree.c:748-756` — `tree_restore_recurse(...)` has no depth parameter;
  `tree.c:887-892` — unconditional self-recursion per `subtree` node.
- `tree.c:781` — `malloc(sizeof(lcsas_json_tok) * 65536)` (≈2.6 MB; `lcsas_json_tok` is
  40 bytes per `json_q.h:32-39`); freed only at `out:` (`tree.c:986-987`), i.e. AFTER the
  child loop — heap held per live level.
- `tree.c:808-810` — per-frame `name_buf[1024]`, `type_buf[32]`, `node_path[4096]`
  (~5.3 KB stack/frame); `tree.c:838` — `snprintf(node_path, sizeof node_path, "%s/%s", ...)`
  return value ignored: paths over 4096 bytes silently truncate (a second, quieter
  deep-tree failure that hits before the stack does).
- `grep -n ulimit recovery/scripts/restore.sh` — no matches.
- `ls recovery/fuzz/*.c` — `fuzz_b64.c fuzz_json_parse.c fuzz_path_safe.c
  fuzz_repo_strip_v2.c fuzz_zstd_decode.c`; no tree harness.
- `recovery/docs/AUDIT_FINDINGS.md:113` — tree.c 89.2%: "would need an attacker-crafted
  fixture, or a fuzz target on `lcsas_tree_restore` directly."
- `tests/recovery_hardening/test_tier1_vs_tier2_differential.py:71-76` —
  `_profile_deep_tree` is 6 levels.

## Fix design

**Depth cap, not an explicit work-stack rewrite.** A heap work-stack would still hold
O(depth) token buffers (children are walked from the parent's live `toks`), so it buys
no asymptotic win — only a riskier restructuring of the binary's most-audited loop. A
hard cap that fails loud is ~20 lines and converts SIGSEGV into a named, actionable error.

1. **`tree.c`** — add `int depth` as the last parameter of `tree_restore_recurse`;
   `lcsas_tree_restore` (tree.c:1009) passes 0; the recursion site (tree.c:887) passes
   `depth + 1`. At function entry:
   ```c
   if (depth > lcsas_tree_max_depth) {
       fprintf(stderr,
           "ERROR: directory tree deeper than %d levels at %s\n"
           "       tier-1 caps recursion to avoid crashing; if this depth is\n"
           "       genuine, re-run with LCSAS_MAX_TREE_DEPTH=<n> and\n"
           "       'ulimit -s unlimited', or use tier-2 (rustic)\n",
           lcsas_tree_max_depth, target_dir);
       return -1;
   }
   ```
   `lcsas_tree_max_depth` defaults to **1000**: ~6 KB/frame × 1000 ≈ 6 MB, inside the
   default 8 MB stack with margin, and `node_path[4096]`/PATH_MAX bound restorable depth
   to roughly that order anyway. `main.c` reads optional `LCSAS_MAX_TREE_DEPTH` (document
   it in restore.sh's env-var table so `test_env_var_docs.py` stays green).
2. **`tree.c:838`** — check the `snprintf` return: if `>= sizeof node_path`, print
   `ERROR: restored path too long (>4095 bytes): %.200s...` and `goto out` (rc −1).
   Truncation is silent wrong-path restoration; it must fail loud like everything else.
3. **`tree.c:1-11`** — rewrite the header comment: recursion is depth-capped at
   `lcsas_tree_max_depth`; delete the `ulimit -s unlimited` prescription.
4. **`recovery/fuzz/fuzz_tree_restore.c`** — link-seam harness, no source refactor
   needed: tree.c's only repo.c dependencies are `lcsas_blob_index_find` (tree.c:635,770)
   and `lcsas_repo_read_blob` (tree.c:641,775). Compile tree.c + path.c + json_q.c +
   hex.c + lcsas_io.c + b64.c with the harness providing stub definitions: `_find`
   returns a static dummy `lcsas_blob_loc`; `_read_blob` returns a copy of the fuzz input
   for the first (root-tree) call and a fixed 4-byte payload for every later call, with a
   per-iteration call budget (~64) so self-referencing `subtree` ids terminate. Restore
   target: a per-run dir under `$TMPDIR`, recursively removed each iteration. Seed corpus
   `recovery/fuzz/corpus/tree/`: one real tree JSON from `gen_fixture.py`, the broken-tree
   fixtures, a `subtree`-cycle, a NUL-escaped name (T1C-03), and a 40-digit number
   (T1C-02).
5. **`recovery/Makefile`** — `fuzz-tree-smoke`/`fuzz-tree` targets cloned from the
   `fuzz_json_parse` pattern (Makefile:625-649); add `fuzz-tree-smoke` to the `fuzz-smoke`
   aggregate (Makefile:802) and update the "5 harnesses" labels (Makefile:816,837) to 6.

No format or catalog impact; read-side only. Old binaries on burned meta discs keep the
old behaviour forever — the fix ships with the next `recovery/bin/*` regeneration.

## Tests & gates

- `tests/recovery_hardening/test_tier1_deep_tree.py` (new; petabyte-test pattern: real
  binary, skip if absent):
  - 900-level tree, 1-char dir names (path ≈1.8 KB, under both caps): exits 0, the
    bottom file restores byte-identical.
  - 2,000-level tree: exits **non-zero**, `returncode > 0` (assert not a signal, i.e.
    not -11), stderr matches `deeper than 1000 levels`.
  - With `LCSAS_MAX_TREE_DEPTH=3000` + `resource.setrlimit(RLIMIT_STACK, unlimited)` in
    the harness: the 2,000-level tree restores fully (proves the override path).
- `recovery/tests/test_repo.c` (or new `test_tree.c`): node whose joined path exceeds
  4096 bytes → restore fails with the path-too-long error, no truncated file created.
  Runs in `make -C recovery test` → audit-gate steps 1/3, always-on for recovery paths
  in `.github/workflows/audit-gate.yml`.
- Fuzz: `make -C recovery fuzz-tree-smoke` (60 s, `-fsanitize=fuzzer,address,undefined`)
  inside `fuzz-smoke` → audit-gate step 4, always-on. Run a one-off 30-minute
  `fuzz-tree` deep run before first merge and triage any findings.
- The deep-tree pytest is local-only until the GATE recovery-hardening-in-CI plan lands.

## Acceptance criteria

- [ ] No input — any depth, any malformed tree JSON — terminates `lcsas-restore` by
      signal; deep trees produce the named depth error (verify: 2,000-level fixture).
- [ ] 900-level tree restores byte-identical with default settings; 2,000-level restores
      with `LCSAS_MAX_TREE_DEPTH` + raised stack ulimit.
- [ ] `tree.c` header no longer prescribes `ulimit -s unlimited`.
- [ ] Over-4096-byte path fails loud; no truncated-path file appears.
- [ ] `fuzz-tree-smoke` green in `fuzz-smoke`; 30-min `fuzz-tree` run completed once
      with 0 unresolved crashes; tree.c coverage in `coverage-c` rises above 89.2%.
- [ ] `make -C recovery audit-gate` green.

## Dependencies & related plans

- **T1C-01** (adaptive token buffers) — land first: its small-start `parse_alloc` shrinks
  the per-level heap from ~2.6 MB to blob-sized, which is what makes a 1000-level cap
  memory-safe; the fuzz harness then exercises the new parse path too.
- **T1C-03** (NUL-in-name) — same tree.c node loop; coordinate merges; its NUL seed
  belongs in this corpus.
- **GATE** "wire recovery-hardening/e2e into CI" — promotes the deep-tree pytest from
  local-only to merge gate.
- After merge: regenerate `recovery/bin/*` (GATE binary-staleness plan).

## Effort

2 days: 0.5 depth cap + path-length guard + tests, 1.0 fuzz harness + Makefile wiring +
corpus + one deep run, 0.5 triage/coverage. Local clang toolchain only; qemu re-verify
of cross-built bins per the usual recovery flow.
