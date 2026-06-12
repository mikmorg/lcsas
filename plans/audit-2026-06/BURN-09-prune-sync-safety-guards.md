# BURN-09: Prune-sync guards — incomplete scans, mass-prune threshold, unprune

**Priority:** P2 · **Severity:** medium · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Guard prune-sync against partial scans; add mass-prune confirm and unprune

## Problem

`lcsas scan` auto-marks packs pruned whenever they are absent from the mirror scan
(default on; opt-out `--no-prune-sync`). The scanner swallows `PermissionError`
per `data/XY` subdirectory and continues, so a transient NAS permission or mount
glitch yields a **non-empty** scan missing whole hash-prefix ranges — defeating
`detect_pruned`'s only guard, which checks the fully-empty case. Wrongly pruned
packs are then excluded from consolidation migration and from
`check_deprecation_safe`: a later consolidate+deprecate cycle can deprecate the
only discs holding them, leaving live data with zero copies while every report
says it was pruned intentionally. There is no un-prune command and no
threshold/confirmation when one scan suddenly prunes thousands of packs.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/packs/scanner.py:70-73` — `except PermissionError ... continue` per
  subdir (the swallowing is even pinned by
  `tests/unit/test_scanner_delta.py:63-73`).
- `src/lcsas/packs/delta.py:95-104` — guard only `if not self._scanner_result`
  (fully-empty case).
- `src/lcsas/cli/main.py:818-829` — auto `bulk_mark_pruned` in `cmd_scan`, no
  threshold or confirmation.
- `src/lcsas/db/queries.py:384-388` — consolidation source
  (`get_packs_only_on_volumes`) filters `is_pruned = 0`;
  `src/lcsas/db/volumes.py:250-255` — `check_deprecation_safe` ignores pruned
  packs.
- `src/lcsas/db/packs.py` — `mark_pruned`/`bulk_mark_pruned` exist (71, 79);
  no reset path anywhere.

## Fix design

No schema change (the two-consecutive-scans idea would need a `missing_since`
column; deferred — the three guards below close the realistic triggers).

1. **Scan completeness signal** (`packs/scanner.py`): introduce
   ```python
   @dataclass(frozen=True)
   class MirrorScanResult:
       packs: dict[str, int]
       errors: list[str]        # paths that raised PermissionError/OSError
   ```
   `scan_mirror_packs` returns it; every `except PermissionError/OSError` branch
   (57-59, 71-73, 81-84) appends to `errors` instead of only logging. Update the
   two call sites (`cmd_scan`, `DeltaAnalyzer` construction) — keep
   `DeltaAnalyzer`'s input a plain dict; the completeness decision lives in
   `cmd_scan`.
2. **Incomplete scan disables prune-sync** (`cli/main.py::cmd_scan`): when
   `result.errors` is non-empty, skip the prune-sync block for that repo and log:
   `"Scan of <repo> was INCOMPLETE (<n> unreadable path(s)) — prune-sync skipped
   for this repo. Fix permissions/mount and re-scan."` New packs still register
   (registration is additive and safe).
3. **Mass-prune threshold**: before `bulk_mark_pruned`, compute
   `len(pruned)` vs repo active count; if `len(pruned) > max(10, 20% of active)`,
   refuse unless `--yes-prune` was passed:
   `"Refusing to mark <n>/<total> packs of <repo> pruned in one scan — this
   usually means the mirror is partially unavailable. Re-run with --yes-prune to
   confirm rustic really pruned them."` Add `--yes-prune` to the scan parser.
4. **Unprune path**: `db/packs.py::unmark_pruned(conn, pack_id)`
   (`UPDATE packs SET is_pruned = 0, pruned_at = NULL WHERE pack_id = ?` —
   match actual column names) + CLI `lcsas pack unprune <sha256-prefix>`
   (resolve prefix, refuse on ambiguity). This is the recovery tool for any past
   mis-prune.

Compat: `is_pruned` semantics unchanged; old on-disc catalogs unaffected.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml):

- `tests/unit/test_cli_scan.py::test_partial_scan_does_not_mark_pruned` —
  monkeypatch `os.scandir` to raise `PermissionError` for one `data/XY` subdir;
  run `cmd_scan`; assert zero packs marked pruned and the INCOMPLETE warning
  logged.
- `tests/unit/test_cli_scan.py::test_mass_prune_requires_confirmation` — scan
  returning 1 of 100 known packs must not prune without `--yes-prune`; with the
  flag it prunes.
- `tests/unit/test_cli_scan.py::test_small_prune_still_automatic` — 2 of 100
  absent → pruned without prompt (pins the threshold, not a blanket prompt).
- `tests/unit/test_db_packs.py::test_unmark_pruned_roundtrip` — mark, unmark,
  back in `get_unarchived_packs`.
- `tests/unit/test_scanner_delta.py` — update the existing
  PermissionError-swallowing test (63-73) to assert the error is *reported* in
  `MirrorScanResult.errors`.
- `tests/unit/test_consolidate.py::test_consolidation_surfaces_pruned_left_behind`
  — a consolidation plan over a volume holding pruned packs must list them in
  its report output, not silently exclude.

## Acceptance criteria

- [ ] A scan with any unreadable subdirectory prunes nothing for that repo and
      says so.
- [ ] A scan that would prune >20% of a repo (or >10 packs) requires
      `--yes-prune`.
- [ ] `lcsas pack unprune <sha>` restores a pack to the active pool.
- [ ] Consolidation reports pruned packs left behind on source volumes.

## Dependencies & related plans

- **BURN-10** (replica truth from ACTIVE copies) — both feed
  `check_deprecation_safe`; independent, either order.
- **FMA** blast-radius reporting — `unprune` + the incomplete-scan signal reduce
  the false-negative inputs to that report.

## Effort

1.5 days: 0.75 impl (scanner result type ripples to two call sites), 0.75 tests.
No special environment.

---
**Implemented:** 2026-06-12. As planned; additionally added a `lcsas pack unprune` CLI test class, and the MirrorScanResult return-type ripple covered all integration/e2e test call sites (not just the two src call sites).
