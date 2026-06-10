# BURN-06: Stop auto-deleting ISOs after the first burn — multi-location re-burn is broken

**Priority:** P1 · **Severity:** high · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Retain session ISOs until clean; unbreak multi-location re-burn

## Problem

`burn_session` deletes each ISO immediately after a successful verified burn "to
free staging space". But the same function explicitly supports re-burning the same
session at a second location (`is_reburn` branch: "just add another copy"), and its
first action requires the ISO to exist — raising `FileNotFoundError` with the
misleading hint "Was the staging directory cleaned prematurely?". So the
**documented primary multi-copy flow** — README Steps 4–5: `burn --session latest
--location Home_Shelf`, then `--location Offsite_Safe`, "Burns the same staged
ISOs again. No re-staging" — always fails right after the first real burn
succeeds. The 2+ physical copies the durability model depends on can only be
produced by re-staging everything from scratch (new volume labels, full ISO+ECC
redo), which the catalog then records as *different* volumes.

The unit tests pass only because both multi-location tests use `skip_burn=True`,
which skips both the deletion and the existence check — the real path is untested.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/burn/orchestrator.py:789-800` — `if verify_passed and not skip_burn
  and iso_path.exists(): iso_path.unlink()` ("Remove ISO after successful verified
  burn to free staging space").
- `orchestrator.py:682-687` — `if not skip_burn and not iso_path.exists(): raise
  FileNotFoundError(... "Was the staging directory cleaned prematurely?")`.
- `orchestrator.py:690-697` — the `is_reburn` multi-location support that can
  never be reached with a real burn.
- `README.md:245-266` — Steps 4–6 document burn→burn→clean as the primary flow;
  Step 6 ("Remove staged ISOs **after all copies are burned**") is exactly the
  deletion point the auto-unlink usurps.
- Test gap: `tests/unit/test_session_pipeline.py:393-413` and `:995-1016` — both
  multi-location tests use `skip_burn=True`; the only `skip_burn=False` tests
  (475-517) burn a single location.

## Fix design

**Chosen design: remove the auto-unlink entirely.** ISO deletion belongs to
`clean_session` — the explicit, documented Step 6 — which after BURN-03 also
guards against cleaning unburned work. The alternative (delete once copies exist
at ≥ `min_copies` locations) was rejected: no `min_copies` knob exists in
`LCSASConfig` today, the operator's intended copy count isn't modeled anywhere,
and a heuristic deletion would still surprise anyone burning a third copy.
Disk-space pressure is already handled by the `stage()` pre-flight (and its
BURN-07 fix); an operator who needs space early runs `lcsas stage --clean`.

Changes in `src/lcsas/burn/orchestrator.py`:

1. Delete the unlink block (789-800) and its comment.
2. Rewrite the `FileNotFoundError` message (683-687) for the remaining ways the
   ISO can be missing:
   `"ISO file missing for volume <label>: <path>. The session's staging area was
   cleaned ('lcsas stage --clean') or the file was removed manually. To burn
   another copy, re-stage for the target location:
   lcsas stage --for-location <location> && lcsas burn --session latest
   --location <location>"`.
3. `cmd_burn_session` summary: after a successful burn, print a reminder —
   `"ISOs retained for additional copies. After burning all locations, free the
   staging space with: lcsas stage --clean"`.
4. README Step 4 comment block: note the ISOs persist until Step 6 (it already
   implies this; make it explicit).

No schema or catalog change; no migration.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml), `tests/unit/test_session_pipeline.py`:

- `test_multi_location_reburn_with_real_burn_path` — `skip_burn=False` with
  mocked `burn_iso` and `verify_disc=True` (mirror the existing 470-487 fixture);
  burn to Home_Shelf then Offsite_Safe; assert the second call succeeds and both
  locations have ACTIVE copies. **Write this test first — it fails on current
  master, pinning the bug.**
- `test_iso_retained_after_verified_burn` — after burn #1 (real path), assert
  every `sv.iso_path` still exists.
- `test_burn_missing_iso_error_mentions_restage` — delete the ISO, assert the new
  error message names `stage --for-location`.
- Keep/extend the two `skip_burn=True` multi-location tests as-is (they cover the
  catalog bookkeeping).

## Acceptance criteria

- [ ] README Steps 4–5 work verbatim against a real session (mock-burn unit test
      proves the code path; the cdemu e2e leg from BURN-04 can exercise it for
      real).
- [ ] No code path deletes an ISO except `clean_session` / `abort_session`.
- [ ] The missing-ISO error names the actual cause and the `--for-location`
      recovery command.
- [ ] `make test-unit` green, including the previously-failing reburn test.

## Dependencies & related plans

- **BURN-03** — makes `clean_session` safe to be the sole deletion point (guard +
  `--force`); land BURN-03 first.
- **BURN-05** — failed-verify retries need the ISO present; this plan guarantees
  it.
- **FUP-01** (burn-operator-protocol follow-up) — documents the burn→burn→clean
  operator rhythm this plan restores.

## Effort

1 day: 0.3 impl, 0.7 tests (the real-burn-path fixture is the work). No special
environment.
