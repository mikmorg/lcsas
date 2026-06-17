# RST-03: One corrupt/missing blob aborts the entire tier-3 restore

> **STATUS: RESOLVED** — landed in `79b1392` (restore: tolerant tier-3 traversal — skip bad blobs, write failure manifest, atomic writes [RST-03]); guarded by `tests/unit/test_standalone_subprocess.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Tier-3 restore: skip failed files, write failure manifest, atomic file writes

## Problem

`PurePythonRestorer` — tier 3, the last-resort restorer shipped as `standalone_restorer.py` on
every data disc — has zero error tolerance during tree traversal. A single `IntegrityError`
(corrupt blob), `KeyError` (blob missing from index), or `FileNotFoundError` (pack absent after
disc-swap retries are exhausted) propagates up and kills the whole restore — possibly hours in
at ~1 MB/s. It leaves a truncated file at the final path (no marker distinguishing it from a
complete file), no record of what was already restored, and no list of what remains.

For the non-technical-heir bar this is the worst possible failure shape on the last-resort tier:
one bad blob → 0% restored, when 99.9% of the family archive is intact and readable. The heir
running the bundled script sees a raw Python traceback (the generated CLI has no exception
handling at all); via `lcsas restore standalone` they see "Pure-Python restore failed: <exc>",
exit 1. RST-02 shows a *format-legal* way to hit this on undamaged discs; physical disc damage
past the ECC margin is the other route.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/restore/restic_fallback.py:977-1060` — `_restore_tree`: no try/except around
  `self._read_blob(tree_id)`, `self._restore_file(node, node_path)`, or the subtree recursion.
- `src/lcsas/restore/restic_fallback.py:1062-1074` — `_restore_file` writes chunks directly to
  the final path (`with open(path, "wb") as f:` ... `f.write(chunk)`); an exception mid-file
  leaves a truncated file with no marker.
- `src/lcsas/restore/restic_fallback.py:468-469` — `restore()` calls
  `self._restore_tree(snap.tree, target)` bare.
- `src/lcsas/cli/main.py:2657-2664` — `except Exception as exc: logger.error("Pure-Python
  restore failed: %s", exc); return 1`.
- `src/lcsas/restore/standalone_builder.py` `_CLI_BLOCK` (ends ~line 263) — final line
  `restorer.restore(target=target, snapshot_id=args.snapshot)` with no try/except: raw traceback
  for the heir.
- Partial mitigation exists only for missing packs: the interactive disc-swap prompt
  (`_find_pack_path`); corrupt-blob and missing-index-entry have none.

## Fix design

Add tolerant traversal, default **ON** (it is the last-resort tier; all-or-nothing is the wrong
default there). Per-node containment, failure manifest, atomic writes:

1. **State** — on `PurePythonRestorer.__init__`/`restore()`: `self._failures:
   list[tuple[str, str, str]] = []` (relative path, blob_id-or-"", reason) and a
   `strict: bool = False` ctor flag for callers who want the old raise-first behavior.

2. **`_restore_file`** (`restic_fallback.py:1062`) — write to `path.with_name(path.name +
   ".lcsas-partial")`, then `os.replace(tmp, path)` after the last chunk and metadata
   application. On exception: unlink the partial, record the failure, re-raise a private
   `_NodeFailed` so the caller logs uniformly. Partial files are therefore never mistaken for
   complete ones, and a re-run is idempotent.

3. **`_restore_tree`** (`restic_fallback.py:985` loop body) — wrap each node's handling:
   ```python
   try:
       ...existing per-node logic...
   except (IntegrityError, KeyError, OSError, FileNotFoundError) as exc:
       if self._strict:
           raise
       self._failures.append((str(node_path), blob_ref, f"{type(exc).__name__}: {exc}"))
       _log(f"FAILED (continuing): {node_path} — {exc}")
       continue
   ```
   A failed **tree blob** read (`self._read_blob(tree_id)` at :979) records one failure for the
   whole subtree path and returns — the parent traversal continues. Keep KeyboardInterrupt and
   the disc-swap FileNotFoundError prompt flow working: the prompt fires first; only *exhausted*
   retries become a recorded failure.

4. **`restore()`** (`:429`) — after traversal, if `self._failures`: write
   `<target>/RESTORE_FAILURES.txt` (one line per failure: path, reason, blob id) and `_log` a
   plain-English summary: `"Restored 9,742 of 9,745 files. 3 file(s) FAILED — see
   RESTORE_FAILURES.txt. The rest of your data is intact in <target>."` Return value gains the
   failure count: add `failures: int` to `SnapshotMeta` return path or (simpler, chosen) raise
   nothing and expose `restorer.failures`.

5. **Callers** — `cli/main.py:2657-2670`: after `restore()`, if `meta`-level failures > 0 →
   log the summary and `return 2` (distinct from hard-failure 1). `_CLI_BLOCK` in
   `standalone_builder.py`: wrap `restorer.restore(...)` in try/except printing a plain-English
   message (no traceback) and `sys.exit(2)` when `restorer.failures`; `sys.exit(1)` on setup
   errors (bad password, missing repo) which should still fail fast.

`standalone_restorer.py` is generated from these sources, so future discs get the behavior
automatically; already-burned discs keep the abort-on-first-error script forever — note in
`recovery/docs/TIERS.txt` that re-running tier-3 from a *newer* meta disc against old data discs
is the remedy.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml):

- `tests/unit/test_restic_fallback.py::test_restore_continues_past_corrupt_blob` — repo fixture
  with 3 files, middle file's data blob corrupted in the pack; assert the other 2 restored
  byte-identical, `RESTORE_FAILURES.txt` names the bad path + reason, summary line printed.
- `tests/unit/test_restic_fallback.py::test_restore_continues_past_missing_index_blob` — drop a
  blob from the index (KeyError path); same assertions.
- `tests/unit/test_restic_fallback.py::test_partial_file_not_left_behind` — corrupt the second
  chunk of a 2-chunk file; assert nothing exists at the final path and no `.lcsas-partial`
  litter remains.
- `tests/unit/test_restic_fallback.py::test_strict_mode_still_raises` — `strict=True` preserves
  the old raise-first contract.
- `tests/unit/test_standalone_subprocess.py` — subprocess run of the generated script against a
  corrupt-blob fixture: assert exit code 2, no traceback on stderr, manifest written.
- `tests/recovery_hardening/` — static guard (style of `test_disc_swap_docs.py`) asserting the
  generated `_CLI_BLOCK` wraps `restore()` and mentions `RESTORE_FAILURES.txt`. Local
  `make test-recovery-hardening` lane (CI wiring is a GATE plan).

## Acceptance criteria

- [ ] Corrupt-one-blob fixture: tier-3 restores all other files, writes RESTORE_FAILURES.txt,
      exits 2 with a plain-English count; never a traceback from the generated script.
- [ ] No truncated file is ever left at a final restore path (verified by the partial-file test).
- [ ] Disc-swap prompt behavior unchanged (existing `test_tier3_disc_swap.py` green).
- [ ] `make test-unit && make lint && make typecheck` pass; regenerated standalone script passes
      `tests/unit/test_standalone_subprocess.py`.

## Dependencies & related plans

- RST-02 (zstd magic) removes the most likely *undamaged-disc* trigger; land in either order.
- RST-09 touches the same `_CLI_BLOCK`; coordinate to regenerate once.
- T1C plans cover the tier-1 C binary's equivalent abort behavior — out of scope here.

## Effort

2 days: 1 impl (traversal + atomic writes + both CLI surfaces), 1 tests (fixture corruption
helpers exist in test_restic_fallback.py already). No special environment.

---
**Implemented:** 2026-06-13. As planned, with two noted deviations:
(1) the internal sentinel is `_NodeFailed` per the plan; ruff N818 is suppressed with a
targeted `# noqa` rather than renaming it. (2) Pre-existing `tests/unit/test_chaos.py`
corruption-rejection tests encoded the old raise-first contract; they now pass `strict=True`
(the documented legacy contract) so they still verify corruption is rejected. New tests added:
4 unit (corrupt/missing-index/partial-litter/strict) + clean-restore no-manifest, 1 subprocess
(exit 2, no traceback, manifest), 1 recovery-hardening static guard
(`test_tier3_tolerant_restore.py`). TIERS.txt documents the tolerant behaviour + the
re-run-from-newer-meta-disc remedy for already-burned discs.
