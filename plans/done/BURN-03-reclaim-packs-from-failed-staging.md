# BURN-03: Compensate failed stages; guard clean_session; add session abort

> **STATUS: RESOLVED** — landed in `6f83722` (burn: compensate failed stages; guard clean_session; add session abort [BURN-03]); guarded by `tests/unit/test_db_queries.py`.

**Priority:** P0 · **Severity:** critical · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Reclaim packs claimed by never-burned volumes; guard stage --clean

## Problem

A pack counts as "archived" the moment a `volume_packs` row exists — the volume's
status is irrelevant. The per-volume commit happens at staging time, *before* the
ISO is mastered, ECC'd, or burned. Three confirmed paths therefore strand packs on
phantom STAGING volumes that will never become discs, with no tool to reclaim them:

(a) **ISO/ECC failure mid-stage:** in `_stage_single_volume`, the compensating
`delete_volume` covers only catalog-injection failure; if xorriso or dvdisaster
fails afterwards, the exception propagates with the committed volume + pack links
intact. (b) **`lcsas stage --clean` on an unburned session:** `clean_session`
unconditionally deletes the ISOs and staging tree of ANY session — including one
never burned — without deleting its volumes or unlinking packs. The only stageable
artifacts are destroyed while the catalog keeps claiming the packs archived.
(c) **Crash window** between the per-volume commit and `add_session_volume`.

In every case, future `lcsas stage` skips those packs forever
(`get_unarchived_packs` excludes any pack with a `volume_packs` row), the
redundancy report counts the phantom STAGING volume as a copy, and restore pick
lists send the heir to a disc that was never burned. Data sits on the NAS, marked
safe, and is silently lost when the NAS dies. The FMA counterpart plan owns the
catalog state-machine semantics (what "archived" should mean); **this plan owns the
pipeline code**: compensation paths, the `clean_session` guard, and the
reclamation tool.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/burn/orchestrator.py:398-401` — `bulk_link_packs(...)` +
  `self._conn.commit()` at stage time.
- `orchestrator.py:407-420` — the only compensation: `try` wraps just
  `wal_checkpoint` + `inject_catalog`; handler calls `delete_volume` (416).
- `orchestrator.py:434-469` — `create_iso`, `augment_iso`, ISO-exists and
  ISO-size checks all raise with **no** compensation; the committed volume +
  `volume_packs` rows survive.
- `orchestrator.py:813-830` — `clean_session`: unlinks every `sv.iso_path`,
  `safe_remove_tree(staging_dir)`, sets session `CLEANED`. No status guard, no
  volume deletion, no pack reclamation.
- `src/lcsas/cli/main.py:1025-1029` — `if args.clean: orch.clean_session(...)`
  unconditional.
- `orchestrator.py:603-637` — crash window: volume committed at 401 inside
  `_stage_single_volume`; `add_session_volume` + commit at 629-637 back in
  `stage()`. A crash between leaves a volume outside `session_volumes` that
  `burn_session` can never see.
- `src/lcsas/db/queries.py:30-47` — archived == any `volume_packs` row;
  `queries.py:168-169,181-182` — pick lists exclude only DEPRECATED/DESTROYED
  (STAGING included); `queries.py:405-417` — redundancy report likewise.
- `orchestrator.py:314-327` — a legacy `abort(manifest)` exists (deletes volume +
  staging) but is wired only to the old single-volume manifest path, not to
  sessions or the CLI.
- Test gap: `tests/unit/test_session_pipeline.py:525-553` (clean tests) assert
  only CLEANED status; `:627-645` asserts only the raise on ISO-creation failure —
  never pack reclamation.

## Fix design

Four changes, all in pipeline code. No schema migration: session status reuses the
existing `CLEANED` value for aborted sessions (the `sessions.status` CHECK allows
only STAGED/PARTIAL/COMPLETE/CLEANED — schema.py:117 — and SQLite CHECKs can't be
altered without a table rebuild; an event/log carries the "aborted" distinction).

### 1. Compensation for ALL post-commit failures in `_stage_single_volume`

Wrap steps 4–7 (catalog injection through ISO size checks, orchestrator.py:403-469)
in a single try/except:

```python
try:
    ... wal_checkpoint / inject_catalog ...
    ... write info files ...
    ... create_iso / augment_iso / size checks ...
except BaseException:
    self._conn.rollback()          # drop any uncommitted partial state
    delete_volume(self._conn, volume.volume_id)   # removes volume_packs too
    raise
```

`delete_volume` (`db/volumes.py:234-238`) already deletes `volume_packs` then the
volume and commits. Keep the existing catalog-injection-specific error message
(re-raise as today inside the broader handler). Use `BaseException` so
KeyboardInterrupt during a multi-minute dvdisaster run also compensates. After
compensation the packs reappear in `get_unarchived_packs` — assert this in tests.
The staging tree is left on disk for diagnosis (the session dir is cleaned by
abort/clean below).

### 2. Shrink the crash window (c)

Move session registration into the same transaction as the volume commit: pass
`session_id` into `_stage_single_volume` and call `add_session_volume(...,
commit=False)` *before* the single `self._conn.commit()` at (current) line 401.
The ISO hash isn't known yet — insert the row with `iso_sha256=''` and add
`update_session_volume_iso(conn, session_id, volume_id, iso_path, iso_sha256)`
in `db/sessions.py`, called from `stage()` after hashing (current 623-637).
Result: a volume row can no longer exist outside `session_volumes`; any crash
afterwards is reachable by `session abort`/doctor below.

### 3. Guard `clean_session` (b)

```python
def clean_session(self, session_ref: str = "latest", *, force: bool = False) -> None:
```

Before deleting anything, collect the session's volumes still in
`('STAGING', 'BURNING')`. If any and not `force`:
```
ValueError: Session <id> has <N> volume(s) that were never burned
(<labels>). Cleaning now would permanently strand their packs as
falsely 'archived'. Burn the session first ('lcsas burn --session <id>'),
or abort it ('lcsas session abort <id>') to return the packs to the
unarchived pool. Use --force to clean AND abort in one step.
```
With `force=True` (and in `session abort`): for each STAGING/BURNING volume,
`delete_volume(...)`, then proceed with ISO/staging-dir removal and set CLEANED.
CLI: add `--force` to the `stage` parser next to `--clean` (cli/main.py:1025).

### 4. `lcsas session abort <ref>` + doctor invariant

- New CLI subcommand `session abort` (a `session` group does not exist yet; add
  `session` with `abort` as its first verb) → `orch.abort_session(session_ref)` =
  `clean_session(session_ref, force=True)` plus an INFO summary: packs reclaimed,
  volumes deleted, bytes returned to the unarchived pool.
- New invariant check, surfaced via `lcsas status`: count packs whose **only**
  volume_packs rows point at STAGING/BURNING volumes older than 24 h, and print a
  loud `WARNING: <N> packs are claimed by never-burned volumes — run 'lcsas
  session abort <id>' or 'lcsas stage --clean --force'` block. Implement as
  `db/queries.py::get_packs_stranded_on_unburned_volumes(conn, older_than_hours)`.
  (A standalone `lcsas doctor` command can absorb this later; `status` is where
  the operator already looks.)
- Stranded volumes from path (c) crashes that predate fix 2 (or from old catalogs)
  have no session — make `abort_session` also accept `--volume <label>` to delete
  a single stranded STAGING volume.

**Compat note (catalog semantics, schema v6):** burned discs carry old catalogs in
which phantom STAGING volumes may exist forever. Restore-side code already
tolerates them (planner prefers VERIFIED/BURNED alternates; worst case a pick-list
entry points at a non-existent disc — the FMA plan owns excluding STAGING volumes
from pick lists/redundancy). `delete_volume` only ever runs against the hot DB.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml), in
`tests/unit/test_session_pipeline.py` unless noted:

- `test_iso_failure_midstage_reclaims_packs` — mock `xorriso.create_iso` to raise
  on volume 2 of 2; assert: exception propagates, volume 1 + its session row
  intact, volume 2 absent from `volumes`, its packs back in
  `get_unarchived_packs()`.
- `test_ecc_failure_midstage_reclaims_packs` — same via `dvdisaster.augment_iso`.
- `test_keyboardinterrupt_during_ecc_compensates` — `augment_iso` raises
  `KeyboardInterrupt`; volume deleted.
- `test_clean_session_unburned_refuses` — stage, then `clean_session()` without
  burning; assert `ValueError` mentioning `session abort`, ISOs still on disk,
  catalog untouched.
- `test_clean_session_force_reclaims` — `clean_session(force=True)`; assert all
  packs unarchived again, volumes deleted, session CLEANED, staging dir gone.
- `test_clean_session_burned_still_works` — burned (skip_burn) session cleans
  without `--force` exactly as today (pins no regression of the documented flow).
- `test_session_volume_row_committed_atomically_with_volume` — monkeypatch
  `sha256_file` in stage() to crash after `_stage_single_volume` returns; assert
  the volume row has a `session_volumes` row (window (c) closed).
- `tests/unit/test_db_queries.py::test_stranded_packs_query` — fixture with a
  STAGING volume claiming packs; `get_packs_stranded_on_unburned_volumes` returns
  them; VERIFIED volumes don't match.
- `tests/unit/test_cli_handlers.py::test_session_abort_cli` — wire-level: abort
  reclaims and prints the summary.

Burn-pipeline fault-injection e2e (audit roadmap test #4) reuses these scenarios
end-to-end; track under the GATE plans.

## Acceptance criteria

- [ ] Killing dvdisaster mid-stage (or any ISO/ECC failure) leaves zero
      volume_packs rows for the failed volume; re-running `lcsas stage` picks the
      packs up again.
- [ ] `lcsas stage --clean` on a never-burned session refuses with the guard
      message; `--force` and `lcsas session abort` reclaim every pack.
- [ ] No code path can create a volume row without a session_volumes row in the
      same transaction.
- [ ] `lcsas status` warns when packs are claimed only by unburned volumes.
- [ ] All listed tests green in `make test-unit` and CI.

## Dependencies & related plans

- **FMA** "packs linked to never-burned STAGING volume counted archived"
  (critical) — owns the lifecycle semantics: what 'archived' means, excluding
  STAGING volumes from pick lists (`queries.py:169,182`) and the redundancy
  report (`queries.py:410`). Coordinate so the queries change lands once.
- **BURN-01/BURN-02** — their failures occur *before* the commit point and need no
  compensation; land in any order, but rebase this plan's line numbers after them.
- **BURN-06** (retain ISO for multi-location burns) — also touches
  `clean_session`'s role as the one sanctioned ISO-deletion point; land BURN-03
  first.
- **FUP-02** (catalog-concurrency follow-up) — the transactional restructuring in
  fix 2 is prerequisite reading for that audit.

## Effort

3 days: 1.5 impl (compensation + transaction restructure + guard + abort CLI +
status warning), 1.5 tests. No special environment.

---
**Implemented:** 2026-06-11. As planned, with three adjustments: the `session`
CLI group already existed (added `abort` as its second verb); `delete_volume`
was extended to remove `session_volumes` rows first (the FK has no ON DELETE
CASCADE, which would otherwise block compensation/force-clean once fix 2
registers session rows at volume-commit time); the three pre-existing
TestCleanSession tests now burn (skip_burn) before cleaning to match the new
guard.
