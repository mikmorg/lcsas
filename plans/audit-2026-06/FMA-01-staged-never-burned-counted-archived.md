# FMA-01: Packs on never-burned STAGING volumes are counted "archived" forever

**Priority:** P0 · **Severity:** critical · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Redefine "archived" to require a burned volume; reclaim ghost-volume packs

## Problem

The catalog considers a pack "archived" the moment a `volume_packs` row exists — i.e. at
staging-commit, before any disc is burned. A volume left in `STAGING` (because the burn was
never run, or because `burn_session()`'s failure path resets it to `STAGING`) keeps its pack
links forever. `clean_session()` then deletes the ISOs and the staging tree but **not** the
volume rows or pack links. Net effect of a mundane operator sequence — `lcsas stage`, burn
fails or is skipped, `lcsas stage --clean` to free the staging SSD — is that those packs are
never burned, never re-selected by the default `lcsas stage` (which only stages "unarchived"
packs), and `lcsas status` reports them archived. The only remaining copy is the hot NAS
mirror; if it dies, the data is gone while the catalog claimed it was safe on disc.

It gets worse on the restore side: the pick-list queries exclude only `DEPRECATED`/`DESTROYED`
volumes, so the ghost `STAGING` volume is offered to an heir as a restore source by label. The
heir hunts through the disc box for a disc that was never burned, with no hint that it cannot
exist. This is the exact inversion of the project's bar — the catalog must never claim more
durability than physically exists.

This plan owns the **lifecycle/semantics redefinition**: what "archived" means, how volume
status is reconciled with physical copies, how existing catalogs are repaired, and the audit
surface. The burn-pipeline code paths that *create* the situation (staging-commit claiming,
`stage --clean` guard internals) are owned by the BURN plan "Packs are permanently 'claimed'
at staging-commit" — implement the two together.

## Evidence

All re-verified against current code (2026-06-10):

- `src/lcsas/db/queries.py:20-48` — `get_unarchived_packs()`: archived ⇔
  `EXISTS (SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id)`. No volume-status or
  copy check. Same pattern in `get_archive_status_summary()` (`queries.py:420-443`).
- `src/lcsas/burn/orchestrator.py:813-830` — `clean_session()` unlinks ISOs, removes the
  staging tree, sets session `CLEANED` — never calls `delete_volume()`. Contrast `abort()`
  at `orchestrator.py:314-326`, which does `delete_volume(self._conn, manifest.volume_id)`.
- `src/lcsas/burn/orchestrator.py:772-787` — `burn_session()` except-path:
  `update_status(self._conn, sv.volume_id, "STAGING")` with `volume_packs` links kept.
- `src/lcsas/db/queries.py:163-184` — `get_pick_list()` filters only
  `v.status NOT IN ('DEPRECATED', 'DESTROYED')` — `STAGING` volumes with zero copies are
  offered as restore sources. Same in `get_pick_list_with_alternates()` (`queries.py:198+`)
  and `get_missing_packs()` (`queries.py:300-314`).
- `src/lcsas/cli/main.py:909-933` — `cmd_status` folds these packs into the "archived" count.
- `src/lcsas/cli/main.py:1025-1029` — `stage --clean` calls `orch.clean_session()` with no
  unburned-session guard.
- Existing tests (`tests/unit/test_session_pipeline.py:524-554`, `TestCleanSession`) assert
  only directory removal and session status — the pack-stranding behavior is unpinned.

## Fix design

**New semantics.** A pack is **archived** iff it is linked to at least one volume whose
status is in the *durable* set `('BURNED', 'VERIFIED', 'CONSOLIDATING')`. A pack linked
*only* to `STAGING`/`BURNING` volumes is **staged** (a new, explicit intermediate bucket).
A pack with no links is **unarchived**. (`CONSOLIDATING` is included because it is only
entered from `VERIFIED` — the disc exists.)

### 1. Query layer — `src/lcsas/db/queries.py`

- Add a module constant `DURABLE_VOLUME_STATUSES = ("BURNED", "VERIFIED", "CONSOLIDATING")`
  and use it everywhere below (single source of truth).
- `get_unarchived_packs()` / `get_total_unarchived_bytes()`: change the `NOT EXISTS`
  subquery to join `volumes` and require `v.status IN DURABLE…`. Packs linked only to ghost
  volumes immediately reappear in the default `lcsas stage` pool — that is the point. A pack
  that *also* has a durable link stays archived (no double-burning).
- `get_archive_status_summary()`: return four buckets — `total, pruned, archived, staged,
  unarchived` — where `staged` = non-pruned packs with links but no durable link. Keep the
  dict keys backward-compatible (`archived` now means durable-archived; add `staged`).
- Pick lists (`get_pick_list`, `get_pick_list_with_alternates`): do **not** exclude
  `STAGING` volumes — see compat note — but (a) order candidate volumes by status rank so a
  durable volume is always preferred over a `STAGING` one for the same pack, and (b) return
  the set of selected volumes that are `STAGING`/`BURNING` with zero `volume_copies` rows so
  the planner can warn.

### 2. Planner surface — `src/lcsas/restore/planner.py`

Add `unconfirmed_volume_labels: dict[str, list[str]]` to `PickList`/`PickListV2` (mirroring
the existing `deprecated_disc_labels` at `planner.py:80,92`). `cmd_restore_plan` prints:

```
WARNING: volume LCSAS-BD25-0042 was staged but has no record of ever being
burned. If you cannot find this disc, it may never have existed. The data it
lists may only exist on the original NAS mirror.
```

### 3. Lifecycle reconciliation — `clean_session()` and a new audit command

- `clean_session()` (`orchestrator.py:813`): for each session volume whose status is
  `STAGING` or `BURNING` **and** which has zero `volume_copies` rows, call
  `delete_volume()` (already exists, `db/volumes.py:234` — removes links + row), logging
  one line per volume: `"Volume <label> was never burned — deleting; N packs return to the
  unarchived pool"`. Volumes with any copy row are left alone. (Pipeline-side guard details
  — confirm prompt on `stage --clean`, mid-stage failure handling — belong to the BURN
  staging-claim plan.)
- New subcommand `lcsas catalog reconcile [--fix]` (`cli/main.py`, next to
  `cmd_catalog_validate` at `main.py:1433`):
  - reports ghost volumes (`STAGING`/`BURNING`, zero copies, older than 24 h), the packs
    stranded on them (count + bytes + repo), and volumes whose status disagrees with their
    copies (durable status but zero `ACTIVE` copies — feeds the volume-status/copies
    reconciliation finding in the BURN family);
  - `--fix` deletes ghost volumes after an interactive `yes` (or `--yes`), printing the
    reclaimed pack count. This is the **migration path for the existing live catalog**,
    which may already contain ghosts.

### 4. Status display — `cmd_status` (`cli/main.py:909-933`)

Print the new bucket: `Packs: N total, A archived, S staged (NOT yet on disc), U unarchived,
P pruned`, and when `S > 0` add one loud line:
`"WARNING: S pack(s) are staged on volumes that were never burned — run 'lcsas catalog
reconcile' or burn the pending session."`

### Migration / compat (schema v6 — no DDL change)

- **No schema change.** This is a query-semantics change only; old and new catalogs share
  the same tables.
- **On-disc catalogs forever contain STAGING ghosts by construction**: every disc's
  holographic catalog is copied at staging time, so the disc's *own* volume appears as
  `STAGING` with zero copies in its own catalog (see FMA-10). Therefore restore-side
  queries **must not exclude** `STAGING` volumes — preference-ordering + warning is the
  only safe behavior. This is why design (a) "warn + prefer durable" beats design (b)
  "exclude STAGING from pick lists": (b) would break every restore that runs against a
  disc-rebuilt catalog.
- Old catalogs read by new code: fine (same tables). New catalogs read by old code: the
  old "archived" overcounts as before — acceptable, no on-disc reader depends on it.

## Tests & gates

All always-on in `make test-unit` (runs in `.github/workflows/test.yml`):

- `tests/unit/test_session_pipeline.py::test_clean_unburned_session_returns_packs_to_unarchived`
  — `stage()`, then `clean_session()`; assert every staged pack reappears in
  `get_unarchived_packs()` and the volume row is gone (`get_volume_by_label` → None).
- `tests/unit/test_session_pipeline.py::test_clean_burned_session_keeps_volumes` — stage,
  `burn_session(skip_burn=True)` + add a copy, clean; assert volumes/links survive.
- `tests/unit/test_db_queries.py::test_unarchived_requires_durable_volume` — pack linked to
  a `STAGING` volume counts unarchived; flipping the volume to `BURNED` flips the pack to
  archived.
- `tests/unit/test_db_queries.py::test_summary_reports_staged_bucket` — archived/staged/
  unarchived buckets partition correctly.
- `tests/unit/test_db_queries.py::test_pick_list_prefers_durable_and_flags_unconfirmed` —
  pack on both a `VERIFIED` and a ghost `STAGING` volume: assigned to the VERIFIED one;
  pack only on the ghost: still offered + label appears in `unconfirmed_volume_labels`
  (the on-disc-catalog compat case).
- `tests/unit/test_cli_handlers.py::test_status_reports_staged_unburned_bucket` — status
  output contains the staged count + warning line.
- `tests/unit/test_cli_handlers.py::test_catalog_reconcile_reports_and_fixes_ghosts` —
  reconcile lists the ghost; `--fix --yes` deletes it and packs return to the pool.
- Burn-pipeline fault-injection e2e (audit roadmap test #4, owned by the BURN plans) must
  include the `stage --clean`-on-unburned-session scenario and assert the catalog never
  reports those packs archived.

## Acceptance criteria

- [ ] `lcsas stage` → `lcsas stage --clean` → `lcsas stage` re-selects the same packs
  (verified end-to-end with `TEST_TINY` media in a unit test).
- [ ] `lcsas status` on a catalog with a ghost volume shows the packs as **staged**, not
  archived, with a warning line.
- [ ] `lcsas catalog reconcile` on the live `archive.db` reports any pre-existing ghost
  volumes; `--fix` reclaims them; a second run reports clean.
- [ ] `lcsas restore plan` against a catalog where a required pack's only source is a
  zero-copy `STAGING` volume prints the "may never have existed" warning.
- [ ] `lcsas restore plan` against an on-disc catalog (own volume = STAGING) still lists
  that disc as a source — no regression in disc-rebuilt-catalog restores.
- [ ] `make test-unit` green; the five new tests run unconditionally in CI.

## Dependencies & related plans

- **BURN — "Packs are permanently 'claimed' at staging-commit"** (critical): owns the
  staging/ISO/ECC failure paths and the `stage --clean` confirmation UX. Land that plan and
  this one in the same release; this plan's query semantics make its reclamation automatic.
- **BURN — "Missing/unmounted mirror silently burns volumes without packs"**: unrelated code
  path, but both feed the fault-injection e2e.
- **FMA-04 — failed verify still records ACTIVE copy**: the reconcile report's
  "durable status with zero ACTIVE copies" check complements it.
- **FMA-10 — burn provenance / holographic gap**: documents *why* on-disc catalogs always
  show their own volume as STAGING; the pick-list compat decision here depends on it.

## Effort

3 days: 1.5 impl (queries + clean_session + reconcile command + status), 1.5 tests
(including the pick-list compat matrix). No special environment; pure-unit.
