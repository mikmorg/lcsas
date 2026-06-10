# FMA-05: No tooled disc-rot re-verification; last_verified_at is a dead column

**Priority:** P2 · **Severity:** medium · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: partial — recovery/docs/READINESS_CHECKLIST.txt "DISC READABILITY SCAN" (manual cadence only; results never recorded)
**Suggested GH issue title:** Add batch disc re-verification that stamps last_verified_at

## Problem

The shelf-storage threat — decades of slow media decay — has no catalog-integrated
detection loop. `burn_session()` deletes each ISO after a verified burn, so the batch
command `lcsas verify --all`, which only knows how to verify ISO *files*, skips essentially
every burned volume ("no ISO path — skipped" / "ISO not found — skipped"). The per-disc
mode is readability-only (FMA-03), and **nothing anywhere writes
`volume_copies.last_verified_at`** — the schema's per-copy freshness field is a dead
column. Neither the owner nor an heir can ask "when was each physical copy last confirmed
good, and which copies are overdue?". The READINESS_CHECKLIST prescribes a monthly manual
`dd`/dvdisaster scan, but its results are never recorded, so holographic catalogs burned
onto later discs carry no verification history.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/burn/orchestrator.py:789-800` — ISO unlinked after a verified burn.
- `src/lcsas/cli/main.py:1818-1833` — `_verify_all` skips any volume without an on-disk ISO
  (`"no ISO path — skipped"` / `"ISO not found ... — skipped"`); only ISO/dvdisaster/
  sha256-of-ISO paths exist.
- `grep -rn last_verified_at src/` — appears only in DDL (`schema.py:103`), migration
  (`schema.py:248-249`), model (`models.py:93`), row reader (`volume_copies.py:19-34`),
  and rebuild column copy (`rebuild.py:186-188`). **No UPDATE writer anywhere.**
- `recovery/docs/READINESS_CHECKLIST.txt:86-102` — manual `dd`/`dvdisaster -t` scan, no
  catalog recording step.

## Fix design

Build on FMA-03's identity+hash disc check (`read_disc_volume_id` + `sha256_device`).

1. **Writer** — `src/lcsas/db/volume_copies.py`:

```python
def stamp_copy_verified(conn, volume_id: int, location: str,
                        when: str | None = None, *, commit: bool = True) -> None:
    """Set last_verified_at on the ACTIVE copy row (volume_id, location)."""
```

2. **Per-disc verify stamps the copy** — `cmd_verify --disc` (`cli/main.py:1713-1769`)
   gains `--location <NAME>` (defaults to the volume's single ACTIVE copy location when
   unambiguous; required otherwise). On PASS: `stamp_copy_verified(...)` in the same
   transaction as the `VERIFY_PASS` event.
3. **Batch mode for physical discs** — `lcsas verify --all --disc [--location X]`:
   iterate ACTIVE copies (at X if given), prompt `"Insert disc <label> and press Enter
   (s = skip, q = quit)"`, run the FMA-03 check, stamp on pass, record `VERIFY_FAIL` +
   no stamp on fail, print a final PASS/FAIL/SKIPPED table. Also fix `_verify_all`'s
   ISO-file path messaging: when no ISO exists, say
   `"<label>: ISO deleted after burn — use 'verify --all --disc' to verify the physical
   disc"` instead of a bare skip.
4. **Staleness report** — `lcsas status --stale-copies [--older-than-days N]` (default
   365): list copies with NULL or older `last_verified_at`, showing label, location, age,
   and `never verified` for NULL. One summary line appears in plain `lcsas status` when any
   copy exceeds the threshold.
5. **Doc** — update READINESS_CHECKLIST "DISC READABILITY SCAN" to run
   `lcsas verify --all --disc` so results land in the catalog (keep `dd` as the
   no-catalog fallback).

**Migration/compat (schema v6 — no DDL change).** The column already exists since v4; old
catalogs simply have NULLs, which the staleness report shows as `never verified`. Readers
on burned discs already tolerate the column (`volume_copies.py:19-21`).

## Tests & gates

Always-on in `make test-unit`:

- `tests/unit/test_cli_handlers.py::test_verify_disc_stamps_last_verified_at` — pass ⇒
  matching copy row stamped; fail ⇒ not stamped.
- `tests/unit/test_cli_handlers.py::test_verify_all_disc_iterates_copies` — fake runner +
  scripted prompts; PASS/FAIL/SKIP table and per-copy stamping asserted.
- `tests/unit/test_cli_handlers.py::test_status_stale_copies_report` — NULL and old
  timestamps listed with ages; fresh copies absent.
- `tests/recovery_hardening/test_last_verified_writer_exists.py` — static test (same
  pattern as `test_env_var_docs.py`): assert a production writer for `last_verified_at`
  exists in `src/lcsas/db/volume_copies.py`, so the column can never regress to dead.
- The CDEmu integration test from FMA-03 gains one batch-mode case (opt-in
  `LCSAS_DISC_VERIFY=1`).

## Acceptance criteria

- [ ] `lcsas verify --all --disc` against CDEmu-loaded discs stamps `last_verified_at` for
  passing copies and records `VERIFY_FAIL` for failing ones.
- [ ] `lcsas status --stale-copies` lists every never-verified copy on the live catalog.
- [ ] `lcsas verify --all` (ISO mode) no longer silently skips burned volumes — it points
  at `--disc`.
- [ ] Static writer-exists test green.

## Dependencies & related plans

- **FMA-03** (content-compare disc verify): hard prerequisite — provides the check this
  batch mode runs.
- **FMA-04**: stamping must only ever touch ACTIVE copies recorded by verified burns.
- **BURN — "Volume status is never reconciled with its physical copies"**: the staleness
  report is a natural place to surface its reconciliation output too.

## Effort

2 days (1 impl, 1 test). CDEmu locally for the opt-in case; unit tests need nothing.
