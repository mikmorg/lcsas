# FMA-04: A failed post-burn verify still records an ACTIVE volume copy

**Priority:** P0 · **Severity:** high · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Never record an ACTIVE copy for a verify-failed burn; stop UPSERT nulling iso_sha256

## Problem

In `burn_session()`, `add_volume_copy()` executes **unconditionally** after the post-burn
verify — a disc that just FAILED read-back verification is recorded as an `ACTIVE` copy at
the location. `volume_copies` has no verify-state column; only a `volume_events` row
distinguishes the bad disc, and nothing queries events. Every location-completeness query
(`get_unarchived_or_missing_at_location`, `get_packs_at_location`) trusts
`vc.status = 'ACTIVE'`, so `lcsas stage --for-location Offsite` will skip those packs
forever: the only Offsite copy is a known-bad disc and the system reports the location
complete. Years later there is no machine-readable record that this physical copy was bad.

A second defect compounds it on re-burns: the UPSERT in `add_volume_copy` forces the
existing row back to `status='ACTIVE'`, overwrites `burn_date`, and overwrites `iso_sha256`
with the incoming value — which the orchestrator **never passes**, so a re-burn (even a
failed one) blanks the stored hash of the previously good copy. That hash is exactly what
the portable SHA-256 verify fallback (`get_iso_sha256_for_label`) and FMA-03's content
verify depend on. A failed attempt to *add* redundancy thus silently destroys the
verifiability of the copy you already had.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/burn/orchestrator.py:723-749` — on `verify_passed = False` the volume is set
  `BURNED` (`:730`), then at `:742-748` `add_volume_copy(self._conn, volume_id=sv.volume_id,
  location=location, commit=False)` runs unconditionally — and without `iso_sha256=` or
  `media_serial=` even though `sv.iso_sha256` is in scope.
- `src/lcsas/db/volume_copies.py:57-67` — UPSERT: `ON CONFLICT(volume_id, location) DO
  UPDATE SET burn_date = excluded.burn_date, ..., status = 'ACTIVE', iso_sha256 =
  excluded.iso_sha256, media_serial = excluded.media_serial` — clobbers a prior good copy's
  hash with NULL and resurrects DEPRECATED rows to ACTIVE.
- `src/lcsas/db/queries.py:446-488` — `get_unarchived_or_missing_at_location` /
  `get_packs_at_location` trust `vc.status = 'ACTIVE'` with no event check.
- `tests/unit/test_burn_orchestrator.py:694-727` — existing re-burn verify-fail test asserts
  only the `VERIFY_FAIL_REBURN` event, not copy state or hash survival.
- Note (verifier): the `catalog import-receipts` path (`cli/main.py:1418` region) DOES pass
  `iso_sha256` — only the local orchestrator path nulls it.

## Fix design

**Chosen design: do not record a copy for a verify-failed burn.** (Rejected alternative: a
`SUSPECT` copy status — `volume_copies.status` has a CHECK constraint
(`schema.py:98-99`), so adding a value means a table-recreating migration that every old
catalog on every burned disc would need tolerated forever; the failed disc should be
re-burned or destroyed, not tracked as a half-copy.)

1. `src/lcsas/burn/orchestrator.py` (`burn_session`, `:742-748`): gate the copy record:

```python
if verify_passed:
    add_volume_copy(
        self._conn,
        volume_id=sv.volume_id,
        location=location,
        iso_sha256=sv.iso_sha256 or None,
        commit=False,
    )
else:
    _logger.error(
        "Verify FAILED for %s at %s — NO copy recorded. The disc in the "
        "drive is not a valid copy: destroy or re-burn it. The ISO has been "
        "kept for re-burning.", vol.label, location,
    )
```

   The ISO is already preserved on verify failure (unlink at `:792` is gated on
   `verify_passed`), so the operator can immediately re-run `lcsas burn` — call that out in
   the message. Apply the same `iso_sha256=` pass-through to the single-volume `burn()`
   path if/where it records copies.

2. `src/lcsas/db/volume_copies.py:57-67` — make the UPSERT non-destructive:

```sql
ON CONFLICT(volume_id, location) DO UPDATE SET
    burn_date    = excluded.burn_date,
    notes        = excluded.notes,
    status       = 'ACTIVE',
    iso_sha256   = COALESCE(excluded.iso_sha256, volume_copies.iso_sha256),
    media_serial = CASE WHEN excluded.media_serial != ''
                        THEN excluded.media_serial
                        ELSE volume_copies.media_serial END
```

   Keeping `status='ACTIVE'` on conflict is correct *now* that the statement only runs for
   verified burns (a deliberate re-burn over a DEPRECATED copy revives it — that is the
   documented re-burn flow). `last_verified_at` is intentionally untouched here (FMA-05
   owns its writer).

3. `get_unarchived_or_missing_at_location` needs no change once (1) lands — the packs at a
   verify-failed location correctly reappear as missing there.

**Migration/compat (schema v6 — no DDL change).** Pure behavior change. Existing catalogs
may already contain ACTIVE copies from past failed verifies and NULLed hashes from past
re-burns; `lcsas catalog reconcile` (FMA-01) should flag copies whose volume has a
`VERIFY_FAIL`/`VERIFY_FAIL_REBURN` event at the same location *newer than* `burn_date`'s
last `VERIFY_PASS`, for manual review. Old on-disc catalogs are read-only history — readers
already tolerate NULL `iso_sha256` (`volume_copies.py:13-25`).

## Tests & gates

Always-on in `make test-unit` / CI `test.yml`:

- `tests/unit/test_burn_orchestrator.py::test_verify_fail_does_not_record_active_copy` —
  fake xorriso with `verify_disc=False`; assert `get_copies_for_volume(active_only=True)`
  empty, `get_unarchived_or_missing_at_location(location)` still returns the packs, and the
  ISO file still exists.
- `tests/unit/test_burn_orchestrator.py::test_verified_burn_records_copy_with_iso_sha256` —
  happy path; copy row's `iso_sha256 == session_volumes.iso_sha256` (this also closes part
  of FMA-10).
- `tests/unit/test_burn_orchestrator.py::test_reburn_verify_fail_preserves_prior_copy` —
  good copy at L1 with hash H; re-burn to L1 with verify fail; assert L1's copy still has
  hash H and original `burn_date`, and a `VERIFY_FAIL_REBURN` event exists (extends the
  existing test at `test_burn_orchestrator.py:694-727`).
- `tests/unit/test_db_volume_copies.py::test_upsert_never_nulls_existing_iso_sha256` —
  direct CRUD: insert with hash, upsert with `iso_sha256=None` ⇒ hash survives; upsert with
  a new hash ⇒ replaced.
- Covered downstream by the burn-pipeline fault-injection e2e (BURN plans): location
  completeness must never be satisfied by a verify-failed disc.

## Acceptance criteria

- [ ] After a verify-failed burn: zero ACTIVE copies at the location, packs reported
  missing at that location, ISO retained, operator message says destroy/re-burn.
- [ ] After a verify-failed *re*-burn: prior copy row byte-identical (hash, burn_date).
- [ ] After a verified burn: copy row carries the session ISO hash (non-NULL).
- [ ] `make test-unit` green with the four new tests.

## Dependencies & related plans

- **BURN — "A failed post-burn verify STILL records an ACTIVE volume copy"**: same finding
  from the burn-pipeline dimension — one implementation; this plan is the design of record.
- **FMA-03 — content-compare verify**: same `burn_session` region; land in the same PR or
  immediately adjacent (FMA-03 makes `verify_passed` meaningful; this plan makes its
  consequences honest).
- **FMA-01 — reconcile command**: hosts the historical-bad-copy audit query.
- **FMA-10 — burn provenance**: the `iso_sha256` pass-through here is half of its fix.

## Effort

1.5 days (0.5 impl, 1 test). No special environment; pure-unit.
