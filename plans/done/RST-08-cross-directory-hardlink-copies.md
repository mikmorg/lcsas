# RST-08: Tier-3 hardlinks reconstructed per-directory only — cross-dir links become full copies

> **STATUS: RESOLVED** — landed in `64b5bde` (restore: share hardlink map across tier-3 tree traversal [RST-08]); guarded by `tests/unit/test_restic_fallback.py`.

**Priority:** P2 · **Severity:** low · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Share hardlink map across tier-3 tree traversal

## Problem

`PurePythonRestorer._restore_tree` creates its `hardlink_map` as a local variable on every call
— i.e., per directory. Two hardlinked files in different directories are each fully
re-extracted instead of linked. Content is correct, but hardlink-heavy archives
(rsnapshot/Time-Machine-style trees, maildirs) can balloon many-fold on restore, risking ENOSPC
partway through a multi-hour tier-3 run — which today aborts the whole restore (RST-03) — and
the heir gets no warning that the target needs more space than the snapshot's logical size.

## Evidence

Re-verified against current code (2026-06-10), `src/lcsas/restore/restic_fallback.py`:

- `:983` — `hardlink_map: dict[int, Path] = {}` declared inside `_restore_tree`.
- `:1027` — `self._restore_tree(node["subtree"], node_path)` — recursion creates a fresh map per
  directory.
- `:1004-1022` — dedup logic keyed on that per-call map (`os.link` + OSError fall-through to
  copy).
- No cross-directory test: hardlink tests in `tests/unit/test_restic_fallback.py` are
  same-directory; `TestHardlinkOSErrorFallback` covers only the OSError fallback.

## Fix design

Thread one shared map through the traversal:

- `_restore_tree(self, tree_id, target_dir, hardlink_map: dict[tuple[int, int], Path] | None
  = None)` — create once at the root call (`restore()` at `:469` passes nothing), pass the same
  dict into the recursion at `:1027`.
- Key by `(node.get("device", 0), inode)` — restic nodes carry `device_id`/`inode`; two equal
  inodes on different source devices must not be cross-linked. Fall back to `inode` alone when
  device is absent (matches current behavior).
- Keep the existing `links > 1` gate and the OSError → copy fall-through verbatim.

Regenerate `standalone_restorer.py` (automatic at next meta/stage build). Old burned discs keep
the copy behavior — content-correct, so no compat action needed.

## Tests & gates

Always-on unit (`make test-unit`):

- `tests/unit/test_restic_fallback.py::test_hardlink_across_directories_restored_as_link` —
  fixture with same inode + `links: 2` in two sibling dirs; assert restored
  `os.stat(...).st_nlink == 2` and both paths share one inode.
- `tests/unit/test_restic_fallback.py::test_hardlink_same_inode_different_device_not_linked` —
  two nodes with equal inode, different `device_id`; assert two independent files.
- Existing same-directory hardlink + OSError-fallback tests must stay green (regression guard
  for the signature change).

## Acceptance criteria

- [ ] Cross-directory hardlinked fixture restores as one inode, two names.
- [ ] Same-inode/different-device fixture restores as two files.
- [ ] `make test-unit && make lint && make typecheck` pass; standalone subprocess tests green
      after regeneration.

## Dependencies & related plans

- Touches the same traversal as RST-03 (skip-and-continue) and RST-06 — land after RST-03 to
  avoid signature churn, and regenerate the standalone script once for the batch.

## Effort

0.5 day. No special environment.

---
**Implemented:** 2026-06-13. As planned: shared (device,inode)-keyed hardlink_map threaded through _restore_tree; two cross-dir + cross-device tests added.
