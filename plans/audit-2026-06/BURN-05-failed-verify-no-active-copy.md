# BURN-05: A failed verify must not record an ACTIVE copy or a COMPLETE session

**Priority:** P0 · **Severity:** high · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Failed post-burn verify must not record ACTIVE copy / COMPLETE session

## Problem

In `burn_session`, when post-burn verification fails the code records a
`VERIFY_FAIL` event and leaves the volume at BURNED — and then **unconditionally**
records a volume copy at the target location (the row's status is `'ACTIVE'`, both
by schema default and by explicit UPSERT), and finally marks the whole session
COMPLETE regardless. Nothing downstream ever acts on the VERIFY_FAIL event.

Consequences: `get_unarchived_or_missing_at_location` and `get_packs_at_location`
filter on `vc.status = 'ACTIVE'`, so the failed disc's packs count as present at
that location — `lcsas stage --for-location Offsite_Safe` will **never re-stage
them for that location**. Location summaries overstate redundancy. For the owner
building the offsite copy that is the heir's lifeline ("the offsite copy is not a
bonus — it is the archive", READINESS_CHECKLIST), one failed burn silently becomes
a permanent phantom copy: the box in the safe is missing a disc the catalog swears
is there, and nobody finds out until the heir does.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/burn/orchestrator.py:704-721` — `verify_passed = False` path adds a
  `VERIFY_FAIL` event only; `:723-730` — volume set to BURNED (not rolled back);
  `:742-749` — "Record copy at location": `add_volume_copy(...)` +
  `self._conn.commit()` run unconditionally after the verify branch.
- `orchestrator.py:803` — `update_session_status(self._conn, session_id,
  "COMPLETE")` regardless of any failed receipts.
- `src/lcsas/db/volume_copies.py:57-68` — UPSERT explicitly sets
  `status = 'ACTIVE'` (line 64); schema default is ACTIVE too.
- `src/lcsas/db/queries.py:454-463, 479-488` — both location queries require
  `vc.status = 'ACTIVE'`; a failed copy matches.
- Test gap: `tests/unit/test_session_pipeline.py:489-517`
  (`test_burn_session_verify_fail_stays_burned`) asserts status + event but never
  asserts no ACTIVE copy was recorded, and never checks session status.

## Fix design

**Chosen design: do not record a copy at all on failed verify.** The alternative
(record with `status='UNVERIFIED'`) requires a `volume_copies.status` CHECK
rebuild (the constraint allows only ACTIVE/DEPRECATED/DESTROYED,
`db/schema.py:94-99`) and every restored/burned old catalog would still lack the
value; a failed disc is simply not a copy, and the `VERIFY_FAIL` volume event
(which carries `location`) already preserves the audit trail.

In `burn_session` (`src/lcsas/burn/orchestrator.py`):

1. Wrap `add_volume_copy` (742-748) in `if verify_passed:`. On failure, log:
   `"Burn at <location> FAILED verification for <label>. NO copy was recorded —
   this location still needs this volume. Inspect the disc/drive and re-run:
   lcsas burn --session <id> --location <location>"`.
2. Track failures across the loop: `any_failed = any(not r.verify_passed for r in
   receipts)`; at line 803 set the session to `"PARTIAL"` instead of `"COMPLETE"`
   when `any_failed`. `PARTIAL` is already in the sessions CHECK
   (`db/schema.py:117`) — **no schema migration needed**.
3. Re-burn path: with no copy row recorded, a retry at the same location takes the
   normal `add_volume_copy` UPSERT path on success — no special handling. The
   `is_reburn` branch (690-697) is unaffected: a VERIFIED volume re-burned at a
   new location that fails verify now also records nothing
   (`VERIFY_FAIL_REBURN` event remains).
4. Surface it: `cmd_burn_session`'s summary output and `lcsas status` should list
   sessions in PARTIAL state and volumes whose latest event is
   VERIFY_FAIL/VERIFY_FAIL_REBURN as "needs re-burn at <location>".

**Compat note:** hot DBs (and on-disc catalogs) written by the old code may
contain ACTIVE copies that never verified. They are indistinguishable from good
copies after the fact; the device-level re-verification from BURN-04
(`lcsas verify --disc` updating `last_verified_at`) is the recovery path — call
that out in the release note for the next burn cycle ("re-verify existing
location discs once").

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml), `tests/unit/test_session_pipeline.py`:

- `test_verify_fail_does_not_create_active_copy` — `verify_disc`→False
  (skip_burn=False, mocked burn); assert `get_copies_for_volume(conn, vid)` is
  empty and `get_unarchived_or_missing_at_location(conn, "Home_Shelf")` still
  returns the volume's packs.
- `test_verify_fail_session_partial_not_complete` — same fixture; assert session
  status == `"PARTIAL"`.
- `test_verify_fail_then_successful_reburn_records_copy` — fail once, then
  succeed; assert exactly one ACTIVE copy and session reaches COMPLETE on the
  second `burn_session`.
- Extend `test_burn_session_verify_fail_stays_burned` (489-517) with the no-copy
  assertion so the original test pins the fix too.
- `tests/unit/test_location_queries.py::test_failed_burn_does_not_satisfy_location`
  — end-state check through `get_packs_at_location`.

## Acceptance criteria

- [ ] After a failed verify, `lcsas stage --for-location <loc>` re-stages the
      affected packs for that location (observable via the location queries).
- [ ] `volume_copies` contains no row for a failed burn; the VERIFY_FAIL event
      carries the location.
- [ ] A session with any failed receipt ends PARTIAL, never COMPLETE; `lcsas
      status` surfaces it.
- [ ] All new/extended tests green in `make test-unit` and CI.

## Dependencies & related plans

- **BURN-04** (device read-back verify) — same function and verify branch; land
  together (one PR) — BURN-04 makes `verify_passed` meaningful, this plan makes
  it consequential. BURN-04's `iso_sha256=` pass-through also fixes the
  UPSERT-blanks-hash defect flagged by the FMA counterpart.
- **FMA** "failed post-burn verify still records an ACTIVE volume copy" — the
  state-machine counterpart; pipeline code changes live here.
- **BURN-06** (ISO retention) — a failed verify means the ISO must still exist for
  the retry; BURN-06 removes the auto-delete that would otherwise race this.

## Effort

1 day: 0.4 impl, 0.6 tests. No special environment.
