# BURN-10: Replica safety math must count ACTIVE copies, not volume status

> **STATUS: RESOLVED** — landed in `5c82769` (db+cli: replica truth counts ACTIVE copies, not volume status [BURN-10]); guarded by `tests/unit/test_db_volume_copies.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked (adjacent: READINESS_CHECKLIST.txt:78-84 covers the operator-side inverse)
**Suggested GH issue title:** Count ACTIVE copies in deprecation guard and redundancy report

## Problem

`deprecate_copy`/`destroy_copy` update only the `volume_copies` row; nothing
demotes the volume when its **last** copy is gone. The two pieces of safety math —
`check_deprecation_safe` (the guard preventing deprecation of a pack's last
replica) and `get_redundancy_report` — count volumes by `volumes.status IN
('BURNED','VERIFIED')` with no join to `volume_copies`. So after the user records
"the only disc of VOL-7 was destroyed", the catalog still treats VOL-7 as a valid
replica: redundancy reports show its packs covered, and deprecating another volume
holding the same packs is permitted — leaving packs with zero physical copies
while every report (including the monthly VOLUME COUNT CHECK in READINESS) says
they are safe.

A confirming nuance from the verifier: `deprecate_copy`/`destroy_copy` have **no
CLI wiring at all** — there is currently no supported way to record a destroyed
disc, which reinforces the gap rather than refuting it (the math is wrong AND the
input path is missing).

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/db/volume_copies.py:220-245` — `deprecate_copy`/`destroy_copy` touch
  only the copy row; no caller anywhere syncs volume status from copies (grep
  confirms no production callers at all).
- `src/lcsas/db/volumes.py:241-265` — `check_deprecation_safe`: `v2.status IN
  ('BURNED', 'VERIFIED')`, no `volume_copies` join.
- `src/lcsas/db/queries.py:397-417` — `get_redundancy_report` joins volumes by
  status only.
- `volume_copies` CHECK allows ACTIVE/DEPRECATED/DESTROYED
  (`db/schema.py:94-99`); `last_verified_at` exists but is never written (fixed
  by BURN-04).

## Fix design

Define replica truth as: **a volume is a live replica iff it has ≥1 ACTIVE copy —
except a volume with zero copy rows at all, which counts by status (legacy).**
The legacy carve-out matters: volumes burned before copies were recorded (and
skip_burn test fixtures, and catalogs rebuilt from old discs) have no
`volume_copies` rows; treating them as dead would mass-trip the deprecation guard.
State this rule in both functions' docstrings.

1. **`check_deprecation_safe`** (`db/volumes.py`): replace the `v2.status` clause
   with:
   ```sql
   AND v2.status IN ('BURNED','VERIFIED')
   AND (
     EXISTS (SELECT 1 FROM volume_copies vc
             WHERE vc.volume_id = v2.volume_id AND vc.status = 'ACTIVE')
     OR NOT EXISTS (SELECT 1 FROM volume_copies vc2
                    WHERE vc2.volume_id = v2.volume_id)
   )
   ```
2. **`get_redundancy_report`** (`db/queries.py:405-417`): same predicate on the
   `volumes` join.
3. **Auto-demote on last copy loss**: in `deprecate_copy` and `destroy_copy`,
   after the UPDATE, if the volume has ≥1 copy row and zero ACTIVE rows, set
   `volumes.status = 'DEPRECATED'` (existing status; no CHECK migration) and add
   a `NOTE` volume event `"auto-deprecated: last physical copy
   deprecated/destroyed at <location>"`. (`NOTE` is already in the
   `volume_events` CHECK — schema.py:138-141 — so no migration.) The operator
   then sees the packs in `get_redundancy_report` and re-burns via
   `stage --for-location`.
4. **CLI wiring** (`cli/main.py`): `lcsas copy deprecate <label> <location>` and
   `lcsas copy destroy <label> <location>` (resolve label→volume_id; print the
   auto-demotion and a `stage --for-location` hint when the volume is demoted).
   Without this, the whole feature is unreachable.

**Compat note (schema v6, no migration):** query/semantics change only. Old
on-disc catalogs are read-only at restore time and never run this math; rebuilt
hot catalogs may contain copy-row-less volumes, which the legacy carve-out handles
(see the FMA catalog-rebuild plan for the rebuild-side status rules).

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml):

- `tests/unit/test_db_volumes.py::test_deprecation_guard_ignores_volumes_without_active_copies`
  — pack on VOL-A and VOL-B, each with one copy row; destroy VOL-B's copy; assert
  `check_deprecation_safe(VOL-A)` lists the pack.
- `::test_deprecation_guard_legacy_volume_without_copy_rows_counts` — VOL-B
  VERIFIED with zero copy rows; guard treats it as a replica (pins the
  carve-out).
- `tests/unit/test_db_queries.py::test_redundancy_report_counts_active_copies` —
  same fixture; pack appears in `get_redundancy_report(min_copies=2)` after the
  destroy.
- `tests/unit/test_db_volume_copies.py::test_destroy_last_copy_demotes_volume` —
  volume → DEPRECATED + NOTE event; with a second ACTIVE copy elsewhere, no
  demotion.
- `tests/unit/test_cli_handlers.py::test_copy_destroy_cli` — wire-level, asserts
  the re-burn hint is printed on demotion.

## Acceptance criteria

- [ ] Destroying a volume's last copy makes its packs appear in the redundancy
      report and blocks deprecation of their other holders.
- [ ] The volume auto-demotes to DEPRECATED with an audit event.
- [ ] `lcsas copy destroy/deprecate` exist and are the supported way to record a
      lost disc.
- [ ] Legacy volumes without copy rows keep counting as replicas (no false alarms
      on existing catalogs).

## Dependencies & related plans

- **BURN-04** — populates `iso_sha256`/`last_verified_at` on copy rows; together
  copies become the authoritative physical record.
- **FMA** "catalog rebuild resurrects DEPRECATED/DESTROYED volumes" — rebuild
  must preserve copy-status truth or the legacy carve-out gets abused; coordinate.
- **BURN-09** — the other `check_deprecation_safe` input fix (pruned packs).
- **FUP-03** (disc-confidentiality follow-up) — destroyed-disc recording feeds its
  exposure model.

## Effort

1.5 days: 0.75 impl (queries + auto-demote + CLI), 0.75 tests. No special
environment.

---
**Implemented:** 2026-06-12. As planned, plus: `deprecate_copy`/`destroy_copy` now raise ValueError on a missing copy row (loud CLI errors instead of silent no-ops); auto-demote never resurrects an already-DEPRECATED/DESTROYED volume.
