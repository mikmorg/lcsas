# FMA-02: Schema migrations are never executed by any production code path

**Priority:** P1 · **Severity:** high · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Wire migrate() into every catalog open; refuse future schemas

## Problem

`db/schema.py` defines `migrate()` — whose docstring even claims it is safe to call on disc
catalog snapshots — but **nothing in production ever calls it**. Every CLI handler runs
`create_all()` only, which is `CREATE TABLE IF NOT EXISTS` and never alters existing tables.
So catalogs silently never upgrade: the project's own live `archive.db` sits at schema
version 5 with a `volume_events` CHECK constraint that lacks both `VERIFY_FAIL_REBURN` and
`BURN_RECEIPT_IMPORTED`, while the code (`CURRENT_SCHEMA_VERSION = 6`) writes both event
types on specific unhappy paths.

Concretely, on this very catalog today: (a) a re-burn whose post-burn verify fails calls
`add_event(..., "VERIFY_FAIL_REBURN")` → `sqlite3.IntegrityError` → the whole `burn_session`
aborts with a raw traceback, exactly at the moment a bad disc needs recording; (b)
`lcsas catalog import-receipts` (writes `BURN_RECEIPT_IMPORTED`) crashes the same way. The
"owner dies between schema versions" scenario is the steady state, not an edge case — and
every holographic on-disc copy inherits the stale schema forever, so heirs decades later
open catalogs that the then-current code may not have been tested against.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/db/schema.py:7` — `CURRENT_SCHEMA_VERSION = 6`; `schema.py:201-209` —
  `migrate()` defined. `grep -rn migrate src/` → **zero callers** (only the definition and
  docstrings).
- `src/lcsas/db/schema.py:138-141` — current DDL CHECK includes `'VERIFY_FAIL_REBURN'` and
  `'BURN_RECEIPT_IMPORTED'`.
- Live `archive.db` (queried during this planning pass): `schema_version = (5,
  '2026-04-11')`; its `volume_events` CHECK is the 6-type v4-era list — **both new event
  types missing**. The v5→v6 migration (`schema.py:303-326`) recreates `volume_events` from
  current DDL and would fix it — it just never runs.
- `src/lcsas/burn/orchestrator.py:734-740` — writes `VERIFY_FAIL_REBURN` on a re-burn verify
  failure; `src/lcsas/cli/main.py:1418` — writes `BURN_RECEIPT_IMPORTED`. Both
  `IntegrityError` against the live catalog.
- CLI handlers call only `create_all`: `cli/main.py:607,624,653,678,780,919,970,1017,1088,
  1221` (and more) — confirmed by grep.
- `tests/unit/test_db_schema.py` exercises `migrate()` directly (always-on) but never the
  production wiring, so CI cannot catch the gap.

## Fix design

**Single choke point.** Add to `src/lcsas/db/schema.py`:

```python
def ensure_schema(conn: sqlite3.Connection, db_path: Path | str | None = None) -> int:
    """create_all + migrate + future-version guard. The ONLY schema entry
    point production code should call."""
    version = get_schema_version(conn)
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Catalog schema is v{version}, but this LCSAS build understands "
            f"up to v{CURRENT_SCHEMA_VERSION}. Use a newer LCSAS to open this "
            f"catalog (writing with this build could corrupt it)."
        )
    create_all(conn)
    if version and version < CURRENT_SCHEMA_VERSION:
        if _is_readonly(conn):          # disc/RO-mount snapshot: read in compat mode
            _logger.warning("Catalog is v%d (read-only) — running in compat mode", version)
            return version
        return migrate(conn)
    return CURRENT_SCHEMA_VERSION
```

- `_is_readonly(conn)`: `PRAGMA query_only` / attempt `BEGIN IMMEDIATE; ROLLBACK` inside a
  `try` — on-disc catalogs are opened from read-only ISO9660 mounts and **must not** be
  migrated in place. Reader code already tolerates old shapes (`volume_copies._row_to_copy`
  handles missing columns, `volume_copies.py:13-25`); keep that contract.
- Mechanical replace: every `create_all(conn)` call in `src/lcsas/cli/main.py` (~16 sites)
  and `src/lcsas/db/rebuild.py:234` becomes `ensure_schema(conn)`. Keep `create_all` itself
  unchanged (tests use it on fresh DBs).
- `rebuild_catalog()` additionally calls `ensure_schema` on the **output** DB only; source
  disc catalogs are attached read-only and never migrated.
- Operator action recorded in the plan: after merge, run any `lcsas status` once against the
  live `archive.db` (with a copy kept) — it migrates v5→v6 in place.
- Error type: new `SchemaVersionError(RuntimeError)` in `schema.py`; CLI handlers let it
  propagate — the message above is heir-readable.

**Migration/compat note (schema v5/v6).** No new DDL in this plan. The v5→v6 migration
becomes *live* for the first time — its crash-atomicity defects are FMA-07's scope and
**must land before or together with this wiring** (a crash mid-`volume_events` recreation
currently wedges the catalog). Burned discs carry v≤6 catalogs forever; the read-only compat
path above is the permanent answer for them, and the future-version refusal is the answer
for the mirror-image hazard (old code, newer catalog).

## Tests & gates

Always-on in `make test-unit` / `.github/workflows/test.yml`:

- `tests/unit/test_db_schema.py::test_cli_auto_migrates_old_catalog` — build a v5-shaped
  catalog fixture by replaying the historical DDL (v4-era `volume_events` CHECK + version
  row 5), invoke `cmd_status` via the CLI dispatcher; assert
  `get_schema_version() == CURRENT_SCHEMA_VERSION` afterwards and the new CHECK accepts
  `VERIFY_FAIL_REBURN`.
- `tests/unit/test_burn_orchestrator.py::test_reburn_verify_fail_on_v5_catalog` — run the
  re-burn-verify-fail scenario (mirror existing test at `test_burn_orchestrator.py:694-727`)
  against the v5 fixture opened through `ensure_schema`; assert the `VERIFY_FAIL_REBURN`
  event is recorded cleanly (this reproduces today's `IntegrityError` before the fix).
- `tests/unit/test_db_schema.py::test_refuses_future_schema` — version row 99 → opening via
  `ensure_schema` raises `SchemaVersionError` with the "newer LCSAS" wording; no write
  occurs.
- `tests/unit/test_db_schema.py::test_readonly_catalog_not_migrated` — v5 fixture with file
  mode 0444 (or `PRAGMA query_only`): `ensure_schema` returns 5, warns, does not raise.
- Guard against regression: `tests/unit/test_db_schema.py::test_no_bare_create_all_in_cli`
  — static grep-style assert that `src/lcsas/cli/main.py` contains no direct
  `create_all(conn)` call (same pattern as existing static doc tests in
  `tests/recovery_hardening/`).

## Acceptance criteria

- [ ] `grep -rn "create_all(conn)" src/lcsas/cli/` returns nothing; all handlers use
  `ensure_schema`.
- [ ] Running `lcsas status` against a copy of today's live v5 `archive.db` upgrades it to
  v6; a subsequent simulated re-burn verify-failure records `VERIFY_FAIL_REBURN` without
  traceback.
- [ ] `lcsas status` against a v99 catalog exits non-zero with the heir-readable refusal.
- [ ] A read-only v5 catalog opens in compat mode (warning, no exception, no write).
- [ ] All five new tests green in `make test-unit`.

## Dependencies & related plans

- **FMA-07 — table-recreating migrations not crash-atomic**: hard prerequisite (or same PR).
  Wiring auto-migration without it arms the crash window on every catalog open.
- **FMA-01 — staged-never-burned semantics**: independent, but `lcsas catalog reconcile`
  introduced there should call `ensure_schema` too.
- **FMT — tier-1 catalog reader schema forward-compat**: the C-side counterpart of the
  future-version guard; reference only.

## Effort

1.5 days (0.5 impl, 1 test incl. the historical-DDL fixture). No special environment.
