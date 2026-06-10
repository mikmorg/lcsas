# FMA-07: Table-recreating migrations are not crash-atomic; create_all masks the wreck as EMPTY

**Priority:** P1 · **Severity:** medium · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: partial — CODE_REVIEW_CLEANUP.md §14 generic "migration strategy" item (the crash-atomicity hazard itself is untracked)
**Suggested GH issue title:** Make table-recreating migrations crash-atomic; refuse wedged catalogs

## Problem

The v4→v5 and v5→v6 migrations do `RENAME → CREATE → INSERT...SELECT → DROP`. Under Python
sqlite3's legacy isolation the DDL statements run in autocommit (the audit reproduced
`conn.in_transaction == False` right after the RENAME), so a crash between the RENAME and
the final commit leaves a catalog with `volumes_old` (or `volume_events_old`) and **no**
`volumes` table, with `schema_version` still reading the old version. Re-running `migrate()`
then fails with `OperationalError: no such table: volumes`.

The masking failure is worse than the wedge: every CLI command runs `create_all()` first,
which recreates an **empty** `volumes` table via `IF NOT EXISTS`. `lcsas status` then shows
zero volumes and restore plans report everything missing, while all real volume data sits
invisibly in `volumes_old`. This is the "owner dies mid-migration" combo — the NAS catalog
needs manual SQL surgery and nothing tells the heir that (the on-disc holographic copies
remain intact, which is the real mitigation, documented nowhere). The hazard is latent
today only because `migrate()` has no production callers (FMA-02); the moment auto-migration
is wired in, this window arms on every catalog open — which is why this medium is scheduled
P1, coupled to FMA-02.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/db/schema.py:264-298` (v4→v5) and `:303-326` (v5→v6) — RENAME/CREATE/INSERT/
  DROP with `conn.commit()` only at the end of each block; `PRAGMA foreign_keys=OFF` set
  outside any transaction at `:265-266` and `:304-305`.
- `src/lcsas/db/schema.py:171-198` — `create_all()` is pure `CREATE TABLE IF NOT EXISTS`;
  on a wedged catalog it recreates an empty `volumes` and proceeds.
- Audit reproduction (independent): simulated crash post-RENAME ⇒
  `tables=['schema_version','volumes_old',...]`, version=4; `migrate()` ⇒
  `OperationalError 'no such table: volumes'`; `create_all` ⇒ empty `volumes` (0 rows)
  while `volumes_old` holds the data.

## Fix design

1. **Explicit transactions per migration step.** In `migrate()` set
   `conn.isolation_level = None` (true autocommit, no implicit-commit-before-DDL), then for
   each table-recreating block:

```
PRAGMA foreign_keys=OFF            -- must be outside a txn (unchanged)
BEGIN IMMEDIATE
  ALTER TABLE volumes RENAME TO volumes_old
  CREATE TABLE volumes (...)
  INSERT INTO volumes SELECT * FROM volumes_old
  DROP TABLE volumes_old
  CREATE INDEX ...
  INSERT INTO schema_version (version) VALUES (?)
COMMIT
PRAGMA foreign_keys=ON
```

   SQLite DDL is fully transactional, so a crash anywhere inside rolls the whole block back
   to a consistent pre-migration catalog. Restore the connection's prior
   `isolation_level` afterwards. Apply to both v4→v5 and v5→v6 (and make this the template
   for the v7 migration FMA-03 adds).
2. **Wedge detection.** New helper `detect_wedged_migration(conn) -> list[str]` returning
   leftover `*_old` table names (query `sqlite_master`). Call it:
   - at the top of `migrate()` → raise with recovery instructions (new code can't produce
     the state, so leftover means a pre-fix crash):
     `"Catalog contains leftover table(s) {names} from an interrupted schema migration.
     Do NOT continue. Restore the catalog from backup, or recover manually: the original
     data is intact in {names}. See docs/RUNBOOK_migration_recovery.md."`
   - in `create_all()` (and therefore `ensure_schema`) → same refusal, so no CLI command
     can ever present the empty-catalog illusion.
3. **Runbook.** Add `docs/RUNBOOK_migration_recovery.md` with the exact two-statement
   manual recovery (`ALTER TABLE volumes_old RENAME TO volumes` after dropping the empty
   shadow, per version) — for the heir-or-helper who hits a pre-fix wedge.

**Migration/compat (schema v6).** No DDL change; this hardens *how* existing migrations
run. On-disc catalogs are never migrated in place (FMA-02's read-only compat mode), so the
crash window only ever applies to the writable NAS catalog — exactly the one copy whose
loss the holographic design cannot cover until the next burn.

## Tests & gates

Always-on in `make test-unit` (`tests/unit/test_db_schema.py`):

- `test_migration_crash_window_is_atomic` — v4-shaped fixture; monkeypatch to raise (or
  close the connection) immediately after the RENAME executes inside the txn; reopen the
  file: assert `volumes` exists with all rows, no `volumes_old`, version still 4; then
  `migrate()` completes cleanly to `CURRENT_SCHEMA_VERSION`.
- `test_migration_is_in_transaction_during_recreate` — instrument: after the RENAME
  statement, `conn.in_transaction` is True (pins the isolation fix directly).
- `test_create_all_refuses_when_volumes_old_present` — hand-build a wedged catalog
  (`volumes_old` present, no `volumes`); `create_all` and `ensure_schema` raise the
  recovery-instruction error; assert the message names the runbook and **no** empty
  `volumes` table was created.
- `test_migrate_refuses_wedged_catalog` — same fixture; `migrate()` raises the same error
  rather than `OperationalError`.
- Repeat the crash test for the v5→v6 block (`volume_events_old`).

## Acceptance criteria

- [ ] Kill -9 simulation mid-v5-migration leaves a catalog that reopens at v4 with all
  volume rows intact and migrates cleanly on retry.
- [ ] On a hand-wedged catalog, every CLI command fails loud with the runbook pointer; none
  shows an empty catalog.
- [ ] `docs/RUNBOOK_migration_recovery.md` exists and its commands restore the wedged
  fixture (verified manually once; commands quoted in the test as a static check).
- [ ] All new tests green in `make test-unit`.

## Dependencies & related plans

- **FMA-02 — wire migrate() into production**: this plan must land **before or with** it;
  FMA-02 arms the window this plan closes. Same PR is acceptable and preferred.
- **FMA-03 — schema v7 (iso_size_bytes)**: must use the atomic template (it's additive
  `ALTER TABLE`, inherently safe, but the wedge-detection guard still applies).

## Effort

1.5 days (0.5 impl, 1 test — the crash-simulation fixtures are the bulk). No special
environment.
