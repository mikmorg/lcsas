# RST-06: Tier-3 symlink escape guard is dead code

> **STATUS: RESOLVED** — landed in `0aafd27` (restore: fix dead symlink-escape guard in tier-3 [RST-06]); guarded by `tests/unit/test_standalone_subprocess.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: only as the skip-reason text in tests/unit/test_restic_fallback.py:1585-1592 (not in UX_CONCERNS/DEFERRED_WORK/AUDIT_FINDINGS)
**Suggested GH issue title:** Fix dead is_relative_to guard for escaping symlinks in tier-3

## Problem

`_restore_tree` intends to skip relative symlinks that resolve outside the restore target, but
the check calls `resolved.is_relative_to(target_dir.resolve())` and **discards the boolean**
inside a `try/except ValueError`. `Path.is_relative_to` returns a bool and never raises
ValueError, so the `continue` branch is unreachable and every escaping relative symlink is
created. The audit reproduced it: a node with linktarget `../../../../etc` produced a live
symlink resolving outside the target.

The matching unit test exists but is `@pytest.mark.skip`'d with a reason that explicitly states
the branch is dead code — the bug was documented instead of fixed — and a separate
path-traversal test file tests a hand-rolled `relative_to()` re-implementation rather than the
production function, giving false assurance. Risk: a tampered or attacker-influenced repo
restored as root can plant symlinks anywhere; subsequent writes through them escape the restore
sandbox. The same generated code ships in `standalone_restorer.py` on every disc. Medium (not
high) because exploitation requires a repo writer holding the key (blobs are Poly1305-MAC'd) —
the core harm is a guard that claims protection it doesn't provide.

## Evidence

Re-verified against current code (2026-06-10), `src/lcsas/restore/restic_fallback.py:1040-1049`:

```python
resolved = (node_path.parent / link_target).resolve()
try:
    resolved.is_relative_to(target_dir.resolve())   # result unused
except ValueError:                                   # never raised
    _log(...)
    continue                                         # unreachable
```

- `tests/unit/test_restic_fallback.py:1585-1603` —
  `test_symlink_escaping_target_skipped` is skipped: reason "...is_relative_to() returns bool
  (never raises ValueError)... so the except-ValueError branch is dead code...".
- `tests/unit/test_restic_fallback_path_traversal.py:44-64` —
  `test_reject_symlink_target_escaping_tree` re-implements the logic with `relative_to()`
  inline; never calls `PurePythonRestorer._restore_tree`.

## Fix design

In `restic_fallback.py:1041-1049`, drop the try/except and use the boolean:

```python
if not resolved.is_relative_to(target_dir.resolve()):
    _log(
        f"Skipping symlink {node_path.name} with out-of-bounds target "
        f"(would escape to {resolved})"
    )
    continue
```

Policy decision (document in the function docstring and `recovery/docs/TIERS.txt` tier-3 notes):
restic itself restores symlinks verbatim; we deliberately diverge for the last-resort tier —
escaping links are **skipped and logged** (with RST-03's failure manifest landing, also record
them there as `skipped-symlink` entries so fidelity loss is visible). Skipping is chosen over
create-and-warn because tier-3 may run as root from a rescue environment.

`standalone_restorer.py` is generated from this file, so new discs pick the fix up at the next
meta/stage build; discs already burned carry the dead guard forever (acceptable: behavior equals
upstream restic's verbatim restore).

## Tests & gates

- Un-skip `tests/unit/test_restic_fallback.py::TestRestoreTreeSecurityPaths::
  test_symlink_escaping_target_skipped` (assertions already correct: link absent +
  "out-of-bounds" logged). Always-on (`make test-unit`).
- Rewrite `tests/unit/test_restic_fallback_path_traversal.py` symlink cases to call the
  production `_restore_tree` via crafted tree blobs (use `_build_repo_with_tree` from
  test_restic_fallback.py; the audit repro is a template). Keep one in-tree-relative-symlink
  case asserting legitimate links are still created.
- `tests/unit/test_standalone_subprocess.py` — subprocess run of the generated script against an
  escape-symlink fixture; assert no out-of-target symlink exists afterward.

## Acceptance criteria

- [ ] `../../../../etc`-style linktarget produces no symlink under the target and a logged skip.
- [ ] In-tree relative symlinks still restore (existing `test_symlink_overwrites_existing_file`
      etc. green).
- [ ] Zero `@pytest.mark.skip` referencing this branch remains.
- [ ] Generated `standalone_restorer.py` exhibits the same behavior under subprocess test.
- [ ] `make test-unit && make lint && make typecheck` pass.

## Dependencies & related plans

- RST-03 (failure manifest) — record skipped escaping symlinks in RESTORE_FAILURES.txt; if
  RST-03 lands first, add the manifest entry here; otherwise log-only and let RST-03 absorb it.
- Coordinate standalone regeneration with RST-02/03/08/09 (one regeneration covers all).

## Effort

0.5 day including test rewrites. No special environment.

---
**Implemented:** 2026-06-13. As planned: dropped the dead try/except, branch on the `is_relative_to` boolean, skip+log+record escaping relative symlinks as `skipped-symlink` manifest entries (RST-03 already landed). Un-skipped the unit test (now asserts manifest), rewrote the path-traversal file to drive the real `_restore_tree`, added a standalone subprocess escape test. Documented the policy in the `_restore_tree` docstring and `recovery/docs/TIERS.txt`. No C/binary changes; `standalone_restorer.py` is generated, so the fix propagates at next stage/meta build.
