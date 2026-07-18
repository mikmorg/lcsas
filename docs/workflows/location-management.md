# Location Management & Multi-Copy Sync

LCSAS treats *physical location* as a first-class catalog dimension. Every burned
disc is tagged with the location it lives at — home safe,
bank deposit box, off-site colocation, "in transit" — and the catalog tracks
each `(volume, location)` pair as an independent **volume copy**. This lets the
same logical volume exist at N places, and lets LCSAS reason about which
locations are *behind* on archival relative to the master mirror.

Multi-copy redundancy is built on two tables: `locations` (the registry of
named places) and `volume_copies` (the M:N join of "which volume lives where",
with status `ACTIVE` / `DEPRECATED` / `DESTROYED`). To bring a lagging
location up to parity, LCSAS computes the *delta* of packs missing at that
location and stages a fresh set of ISOs containing just those packs —
the `--for-location` flow. The same staging+burn pipeline is reused, so
delta burns are physically indistinguishable from initial burns once the
disc is in hand.

## Table of contents

1. [`lcsas location add`](#lcsas-location-add)
2. [`lcsas location list`](#lcsas-location-list)
3. [`lcsas location status`](#lcsas-location-status)
4. [`lcsas location move`](#lcsas-location-move)
5. [`lcsas copy deprecate` / `lcsas copy destroy`](#lcsas-copy-deprecate--lcsas-copy-destroy)
6. [Multi-copy sync via `--for-location`](#multi-copy-sync-via---for-location)
7. [How `volume_copies` and `locations` interact](#how-volume_copies-and-locations-interact)
8. [Gaps & multi-copy edge cases](#gaps--multi-copy-edge-cases)

---

## `lcsas location add`

**Purpose:** Register a named physical storage location so volume copies can
reference it via foreign key.

**Prerequisites:**
- `--config` pointing at a valid LCSAS config (the command refuses to run
  without it — the config check at the top of `cmd_location()`,
  `src/lcsas/cli/main.py`).
- Writable catalog at `config.db_path` (or `--db` override).

**Steps:**
1. Argparse defines the `location add <name> [--description …]` subcommand
   (the `location` subparser block in `build_parser()`,
   `src/lcsas/cli/main.py`).
2. `cmd_location()` dispatches to the `"add"` branch.
3. The name is sanitized with `sanitize_name(..., "location name")`
   (in the `"add"` branch of `cmd_location()`) to strip path-unsafe
   characters.
4. `create_location(conn, name, description)`
   (`src/lcsas/db/locations.py`) inserts a row into `locations`
   (PK = `name` — the `locations` table DDL in `src/lcsas/db/schema.py`).
5. `INSERT` raises `sqlite3.IntegrityError` on duplicate names — nothing
   catches it inside `cmd_location()`; the top-level handler in `main()`
   reports it as `Unexpected error: …` (suggesting `--verbose` for the
   full traceback) and exits 1.
6. On success, logs `Added location: <name>` (the `"add"` branch of
   `cmd_location()`).

**Expected outcome:** A new row in `locations` with `name`, default empty
`description`, and `created_at = CURRENT_TIMESTAMP`. Future `volume_copies`
inserts may now reference this name (FK
`volume_copies.location → locations.name` — the `volume_copies` table DDL
in `src/lcsas/db/schema.py`).

**Variant axes that apply:**
- Multi-copy — the entire feature exists to enable >1 copy across locations.
- Multi-tenant — locations are global, not per-repo; tenants share the
  same location namespace.

**Test coverage:**
- Existing: `tests/unit/test_db_locations.py::TestLocationCRUD::test_create_and_get`,
  `test_duplicate_raises`, `test_ensure_creates_if_missing`.
- Gap: no CLI-level test covers `sanitize_name` rejecting bad input from
  `location add`.

**Source refs:**
- `build_parser()` (`location` subparser) and the `"add"` branch of
  `cmd_location()` in `src/lcsas/cli/main.py`
- `create_location()` in `src/lcsas/db/locations.py`
- the `locations` table DDL in `src/lcsas/db/schema.py`

---

## `lcsas location list`

**Purpose:** Enumerate every registered location alongside a coverage
summary (volume count, pack count, packs behind master).

**Prerequisites:**
- `--config` with a readable catalog.

**Steps:**
1. Parser registers the bare `location list` subcommand
   (the `location` subparser block in `build_parser()`).
2. `cmd_location()` enters the `"list"` branch
   (`src/lcsas/cli/main.py`).
3. `list_locations(conn)` returns all rows ordered by name
   (`src/lcsas/db/locations.py`).
4. If the table is empty, logs `No locations registered.` and returns 0
   (the `"list"` branch of `cmd_location()`).
5. `get_location_summary(conn)` aggregates per-location volume and pack
   counts, computing `missing = total_archived - pack_count`
   (`src/lcsas/db/queries.py`).
6. The handler joins each location with its summary (defaulting to zeros
   when a location has no copies yet) and emits one line per location
   with a status of either `all current` or `<N> packs behind`
   (the `"list"` branch of `cmd_location()`).

**Expected outcome:** One line per registered location:
`<name>  <N> volumes, <M> packs, all current | K packs behind`.

**Variant axes that apply:**
- Multi-copy — coverage "behind" only makes sense once two or more
  locations exist.
- Multi-tenant — pack counts pool every repo because
  `get_location_summary` doesn't filter by repo.

**Test coverage:**
- Existing: `tests/unit/test_location_queries.py` covers
  `get_location_summary` directly; `tests/unit/test_db_locations.py::
  TestLocationCRUD::test_list_locations` covers ordering.
- Gap: no CLI test exercises the joined summary output formatting.

**Source refs:**
- the `location` subparser in `build_parser()` and the `"list"` branch of
  `cmd_location()` in `src/lcsas/cli/main.py`
- `list_locations()` in `src/lcsas/db/locations.py`
- `get_location_summary()` in `src/lcsas/db/queries.py`

---

## `lcsas location status`

**Purpose:** Show a detailed coverage report for one location — exactly
which packs are present there, which ones are missing, grouped by repo.

**Prerequisites:**
- `--config` with a readable catalog.
- The location must exist in the registry (no explicit check; missing
  locations return empty sets rather than raising).

**Steps:**
1. Parser registers `location status <name>`
   (the `location` subparser block in `build_parser()`).
2. `cmd_location()` enters the `"status"` branch
   (`src/lcsas/cli/main.py`).
3. `get_packs_at_location(conn, name)` returns the set of pack IDs that
   have at least one `ACTIVE` copy at this location, by joining
   `volume_packs` to `volume_copies` filtered on
   `vc.status='ACTIVE'` (`src/lcsas/db/queries.py`).
4. `get_packs_missing_at_location(conn, name)` returns Pack rows that
   are archived somewhere but **not** at this location
   (`src/lcsas/db/queries.py`).
5. The handler logs the totals, then bins missing packs by `repo_id`
   and reports `repo=<id>: <N> packs (<size> GB)`
   (the `"status"` branch of `cmd_location()`).

**Expected outcome:** Output of the form

```
Location: Offsite_Safe
  Packs archived here: 1245
  Packs missing: 73
    repo=family: 52 packs (5.4 GB)
    repo=work:   21 packs (1.9 GB)
```

**Variant axes that apply:**
- Multi-copy — the report is meaningless for a single-location setup
  (always "0 missing").
- Multi-tenant — output is grouped per repo, so multi-repo deployments
  see one line per tenant.

**Test coverage:**
- Existing: `tests/unit/test_location_queries.py::TestLocationQueries`
  covers `get_packs_at_location` and `get_packs_missing_at_location`
  exhaustively (empty, partial, full coverage).
- Gap: no CLI test asserts the per-repo grouping in the output;
  no test covers an unknown location name (currently silently returns
  empty sets, instead of raising).

**Source refs:**
- the `location` subparser in `build_parser()` and the `"status"` branch
  of `cmd_location()` in `src/lcsas/cli/main.py`
- `get_packs_at_location()` and `get_packs_missing_at_location()` in
  `src/lcsas/db/queries.py`

---

## `lcsas location move`

**Purpose:** Update the catalog when a physical disc is moved between two
registered locations (e.g. quarterly rotation from `Home_Safe` to
`Bank_Vault`).

**Prerequisites:**
- Both source and destination locations must exist (FK on
  `volume_copies.location` — the `volume_copies` table DDL in
  `src/lcsas/db/schema.py`).
- An `ACTIVE` `volume_copies` row must exist for
  `(volume_id, from_location)`.
- The destination must **not** already have a copy of this volume —
  the UNIQUE constraint `(volume_id, location)` blocks duplicates
  (`volume_copies` DDL in `src/lcsas/db/schema.py`).

**Steps:**
1. Parser registers `location move <volume_label> --from <a> --to <b>`
   (the `location` subparser block in `build_parser()`).
2. `cmd_location()` enters the `"move"` branch
   (`src/lcsas/cli/main.py`).
3. `get_volume_by_label(conn, args.volume_label)` resolves the label to
   a `Volume` row; missing labels log an error and return 1
   (the `"move"` branch of `cmd_location()`).
4. `move_volume_copy(conn, volume_id, from_location, to_location)`
   issues an `UPDATE volume_copies SET location = ?, notes = … WHERE
   volume_id = ? AND location = ? AND status = 'ACTIVE'`
   (`src/lcsas/db/volume_copies.py`).
5. The append-only audit string `Moved from <a> on <ts>\n` is
   concatenated into `notes` for trace-ability, and a `LOCATION_MOVE`
   row (from/to serialised into `detail`) is inserted into
   `volume_events` in the same transaction (issue #16,
   `move_volume_copy()`).
6. A `sqlite3.IntegrityError` (duplicate destination) is translated
   into a `ValueError` with a user-readable message
   (`move_volume_copy()`).
7. A zero-row update (no `ACTIVE` copy at `from_location`) also raises
   `ValueError` (`move_volume_copy()`).
8. On success, the handler logs `Moved <label>: <a> → <b>`
   (the `"move"` branch of `cmd_location()`).

**Expected outcome:** A single `volume_copies` row mutated in place —
`location = to`, `notes` extended with the move record — plus one
`LOCATION_MOVE` row in `volume_events`. No new copy row is
created; the disc retains its `burn_date`, `iso_sha256`, and
`media_serial`.

**Variant axes that apply:**
- Multi-copy — the entire workflow is multi-copy-specific.
- OS — none; pure catalog mutation, no media handling.

**Test coverage:**
- Existing: `tests/unit/test_db_volume_copies.py` covers
  `move_volume_copy` happy-path and duplicate-destination paths;
  `tests/unit/test_cli_comprehensive.py::test_location_move_nonexistent_volume`
  covers the missing-label error path.
- Gap: no test covers the "no `ACTIVE` copy at source" branch via CLI;
  no test verifies that `notes` accrues across multiple moves.

**Source refs:**
- the `location` subparser in `build_parser()` and the `"move"` branch of
  `cmd_location()` in `src/lcsas/cli/main.py`
- `move_volume_copy()` in `src/lcsas/db/volume_copies.py`
- the `UNIQUE(volume_id, location)` constraint in the `volume_copies`
  DDL, `src/lcsas/db/schema.py`

---

## `lcsas copy deprecate` / `lcsas copy destroy`

**Purpose:** Record the *fate* of a single physical disc copy when a disc
is damaged, retired, or lost. `deprecate` marks a copy as retired-but-
present; `destroy` marks it as gone. Both mutate one `volume_copies` row
(by `(volume_label, location)`), leaving copies at other locations
untouched.

**Prerequisites:**
- `--config` with a writable catalog.
- The volume label must resolve to a `Volume` row. `deprecate` requires
  an `ACTIVE` `volume_copies` row for `(volume, location)`; `destroy`
  accepts a copy row in any status.

**Steps:**
1. Parser registers `copy deprecate <volume_label> <location>` and
   `copy destroy <volume_label> <location>` (both positional)
   in `build_parser()`.
2. `cmd_copy` resolves the label with `get_volume_by_label`; a missing
   label logs an error and returns 1.
3. It dispatches to `deprecate_copy(conn, volume_id, location)` or
   `destroy_copy(conn, volume_id, location)`
   (`src/lcsas/db/volume_copies.py`), which flip the copy's `status` to
   `DEPRECATED` / `DESTROYED`. A `ValueError` (no matching copy — no
   `ACTIVE` copy for `deprecate`, no copy row at all for `destroy`) is
   logged and returns 1.
4. **Auto-demotion:** if that was the *last* `ACTIVE` copy of the volume,
   the helper returns a "demoted" flag and the volume itself drops to
   `DEPRECATED`. `cmd_copy` then warns that the volume's packs now count
   in `lcsas status` / the redundancy report and suggests re-burning via
   `lcsas stage --for-location <location>`.

**Expected outcome:** One `volume_copies` row transitions to
`DEPRECATED` / `DESTROYED`. Losing the last `ACTIVE` copy auto-demotes
the parent volume to `DEPRECATED` so the redundancy report and the
deprecation guard stop counting it as a replica.

**Variant axes that apply:**
- Multi-copy — the command is only meaningful once a volume has copies
  across locations; with a single copy, `destroy` triggers the
  auto-demotion warning.
- Multi-tenant — operates on a volume label, independent of repo.

**Source refs:**
- `build_parser()` (`copy` subparser), `cmd_copy`
  (`src/lcsas/cli/main.py`)
- `src/lcsas/db/volume_copies.py` (`deprecate_copy`, `destroy_copy`)

---

## Multi-copy sync via `--for-location`

**Purpose:** Bring a lagging location up to parity with the master mirror
by staging and burning *only* the packs that location is missing.

**Prerequisites:**
- The target location is already registered (`lcsas location add`). As of
  issue #19, `lcsas burn --location <name>` rejects unknown names rather
  than silently registering them; pass `--create-location` to opt into
  creation during burn.
- The catalog contains the full set of packs (i.e. `lcsas scan` has run
  against the live mirrors).
- Enough free disk under `config.staging_path` to hold the delta ISOs
  plus ECC overhead (the disk-space pre-flight in
  `BurnOrchestrator.stage()`, `src/lcsas/burn/orchestrator.py`).

**Steps:**

1. **Entry point:** `lcsas stage --for-location <name>` — explicit flag
   (the `--for-location` option on the `stage` subparser in
   `build_parser()`, `src/lcsas/cli/main.py`). `cmd_stage()` passes
   `for_location=args.for_location` into `BurnOrchestrator.stage()`.
   Burn the resulting session with
   `lcsas burn --session <id> --location <name>` to tag the burned copies
   for that location.

2. `BurnOrchestrator.stage()` receives `for_location` and routes pack
   selection through `_gather_packs_for_staging`
   (`src/lcsas/burn/orchestrator.py`).

3. `_gather_packs_for_staging()` calls
   `get_unarchived_or_missing_at_location(conn, location)`
   (`src/lcsas/db/queries.py`). This returns:
   - packs not on any volume yet, **plus**
   - packs that are on some volume but have **no** `ACTIVE`
     `volume_copies` row at the target location.

4. If `repo_ids` were also passed, the result is intersected with that
   set (the repo filter at the end of `_gather_packs_for_staging()`).

5. Raises `ValueError("No packs need staging.")` if the location is
   already up to date (`BurnOrchestrator.stage()`).

6. The remaining pipeline is identical to a fresh burn: FFD bin-pack
   (`BurnOrchestrator._multi_bin_pack()`), staging-dir build with
   holographic catalog injection, ISO master via xorriso, ECC via
   DVDisaster, then `burn_session()`.

7. `burn_session(..., location=<name>)`
   (`BurnOrchestrator.burn_session()`) burns each ISO and calls
   `add_volume_copy(conn, volume_id, location)`
   (`src/lcsas/db/volume_copies.py`). The UPSERT means re-running the
   burn at the same location is idempotent: it updates `burn_date`
   instead of failing.

8. For re-burns of a volume that is already `VERIFIED` at another
   location, `burn_session` deliberately skips the
   `VERIFIED → BURNING → VERIFIED` status churn and just records a new
   copy (the `is_reburn` path in `BurnOrchestrator.burn_session()`).

**Expected outcome:** New `volume_copies` rows linking the delta-volume(s)
to the target location, with `status = 'ACTIVE'`. The original volumes
(those satisfying other locations) are untouched in their statuses;
new physical discs are issued **only** for the delta, never duplicating
existing copies at the target.

**Variant axes that apply:**
- Media type — delta size determines how many discs are needed; small
  deltas can fit on a single BD25 even when the original archive used
  MDISC100.
- Multi-copy — the entire feature is multi-copy.
- Multi-tenant — `--repo` filters the delta to a subset of tenants;
  see step 4.
- Optical drive count — irrelevant to selection, relevant to burn-loop
  pacing (one drive = serialized burns).
- ECC — always on for production media; implicitly skipped for TEST_*.
  Has no effect on pack selection.
- Recovery tier — sync targets Tier 2 only.

**Test coverage:**
- Existing:
  - `tests/unit/test_session_pipeline.py::test_stage_for_location_unarchived`
    — staging into a brand-new location.
  - `tests/unit/test_session_pipeline.py::test_stage_for_location_delta`
    — staging the delta when some packs already exist at the target
    location.
  - `tests/unit/test_cross_location_restore.py` — restoring with copies
    spread across locations.
  - `tests/unit/test_location_queries.py` — exhaustive coverage of
    `get_unarchived_or_missing_at_location`.
- Gap: no integration test covers an end-to-end
  `stage --for-location` → `burn --session --location` pair where the
  staging filter and copy tag share a name.

**Source refs:**
- the `--for-location` flag in `build_parser()` and `cmd_stage()` in
  `src/lcsas/cli/main.py`
- `BurnOrchestrator.stage()`, `burn_session()`, and
  `_gather_packs_for_staging()` in `src/lcsas/burn/orchestrator.py`
- `get_unarchived_or_missing_at_location()` in `src/lcsas/db/queries.py`
- `add_volume_copy()` (UPSERT) in `src/lcsas/db/volume_copies.py`

---

## How `volume_copies` and `locations` interact

The two tables form a small star schema around the physical world
(see [`docs/architecture.md`](../architecture.md) for the full catalog
schema, version 9):

```
locations                volume_copies                  volumes
---------                -------------                  -------
name (PK)  ◀─────FK──── location                       volume_id (PK)
                          volume_id     ─────FK────▶
                          status (ACTIVE|DEPRECATED|DESTROYED)
                          burn_date
                          iso_sha256
                          media_serial
                          UNIQUE (volume_id, location)
```

Key behaviours derived from the schema (the `locations` and
`volume_copies` table DDL in `src/lcsas/db/schema.py`):

- `locations.name` is the primary key, used directly as the foreign key
  in `volume_copies` and `volume_events`. Renaming a location requires a
  cascading update (not implemented — see Gaps).
- `volume_copies` has a `UNIQUE(volume_id, location)` constraint and the
  CRUD layer leans into it: `add_volume_copy` uses
  `INSERT … ON CONFLICT(volume_id, location) DO UPDATE SET burn_date=…,
  status='ACTIVE', iso_sha256=…, media_serial=…`
  (`add_volume_copy()` in `src/lcsas/db/volume_copies.py`). Re-burning
  the same volume to the same location is therefore idempotent and
  resurrects deprecated copies.
- Volumes can transition through their own lifecycle
  (`STAGING → BURNING → BURNED → VERIFIED → DEPRECATED → DESTROYED`)
  independently of their copies' statuses. A `VERIFIED` volume re-burned
  to a new location stays `VERIFIED`; only the new copy starts fresh
  (the `is_reburn` path in `BurnOrchestrator.burn_session()`,
  `src/lcsas/burn/orchestrator.py`).
- `volume_events` (table DDL in `src/lcsas/db/schema.py`) keeps an audit
  trail including a `LOCATION_MOVE` event type, and `move_volume_copy()`
  emits one atomically with the copy update (issue #16) — from/to are
  serialised into the event's `detail` column.
- `get_location_summary` computes "packs behind" against the *global*
  archived pack count, so all locations are compared against the union of
  archived packs across every location — not against the live mirror.
- Volume copies cascade-delete with the parent volume
  (`ON DELETE CASCADE` on `volume_copies.volume_id`,
  `src/lcsas/db/schema.py`); location rows
  do **not** cascade — `DELETE FROM locations` with live copies will
  raise an FK error.

---

## Gaps & multi-copy edge cases

The following are intentional or known limitations worth noting before
operating LCSAS in a multi-location production setup:

1. **`location move` is audit-logged (fixed in #16).**
   `move_volume_copy()` inserts a `LOCATION_MOVE` row into
   `volume_events` (from/to serialised into `detail`) in the same
   transaction as the `volume_copies` update, so audit consumers see
   every physical move.

2. **No `location rename` or `location remove`.**
   `delete_location()` exists at the DB layer
   (`src/lcsas/db/locations.py`) but is not wired into the CLI. There
   is no rename operation at all; because `locations.name` is the FK
   target, a rename would have to cascade through `volume_copies` and
   `volume_events` manually.

3. **`location status` doesn't check that the location exists.**
   The `"status"` branch of `cmd_location()` passes an arbitrary name
   straight to queries (`src/lcsas/cli/main.py`); an unknown location
   yields `0 archived / 0 missing` rather than an error.

4. **`location move` cannot swap two discs at once.**
   The UNIQUE `(volume_id, location)` constraint means moving disc A
   from `Home` to `Vault` while moving disc B (same volume) from `Vault`
   to `Home` requires a two-step "park at intermediate location" dance.
   This is by design — there is only one logical "current location"
   per copy — but operators should be aware.

5. **`location move` operates on `ACTIVE` copies only.**
   A `DEPRECATED` or `DESTROYED` copy at the source location is invisible
   to `move_volume_copy` and the command reports
   `No active copy of volume X at '<from>'`
   (the zero-row check in `move_volume_copy()`,
   `src/lcsas/db/volume_copies.py`). Restoring a deprecated copy
   requires a manual SQL update.

6. **`--for-location` always pulls `is_pruned = 0` packs.**
   `get_unarchived_or_missing_at_location` filters out pruned packs
   (the `is_pruned = 0` predicate in its query,
   `src/lcsas/db/queries.py`), so a freshly-pruned pack cannot be
   re-burned to a lagging location even if it still exists on the live
   mirror. For long-rotation off-site copies, run `--for-location`
   *before* `rustic forget --prune`.

7. **`lcsas burn --location` rejects unknown names (fixed in #19).**
   The CLI resolves `--location` via
   `resolve_location(conn, name, create=args.create_location)`
   (`src/lcsas/db/locations.py`). Unknown names abort with a
   "did you mean …?" hint computed by `difflib.get_close_matches`, so
   a typo in `lcsas burn --location Offsite_Safre` no longer mints a
   phantom row. Operators who actually want a new location pass
   `--create-location` (or pre-register with `lcsas location add`).
   `burn_session()` still calls `ensure_location` defensively for
   non-CLI callers, but in the CLI path the row is guaranteed to exist
   by then.

8. **`get_location_summary` ignores `DEPRECATED`/`DESTROYED` volumes**
   (the volume-status filter in `get_location_summary()`,
   `src/lcsas/db/queries.py`), so a location whose discs have all
   been retired shows as `0 volumes, 0 packs, 0 packs behind` rather
   than as a special "obsolete" state. The location row remains in the
   registry until explicitly deleted.

9. **Recording copy fate is supported via `lcsas copy`.**
   The DB layer's `deprecate_copy` / `destroy_copy`
   (`src/lcsas/db/volume_copies.py`) are wired into the CLI as
   `lcsas copy deprecate` and `lcsas copy destroy` (`cmd_copy` in
   `src/lcsas/cli/main.py`). Marking a damaged or lost disc no longer
   requires a Python REPL or direct SQL — see the
   [`lcsas copy`](#lcsas-copy-deprecate--lcsas-copy-destroy) section.

10. **`media_serial` is captured in `volume_copies` but not surfaced.**
    The schema reserves `media_serial` and `last_verified_at` columns
    (the `volume_copies` DDL in `src/lcsas/db/schema.py`).
    `last_verified_at` *is* now written at burn time — the
    `add_volume_copy()` call in `BurnOrchestrator.burn_session()`
    passes the post-burn verification timestamp — and surfaced via
    `lcsas status --stale-copies`. `media_serial`, however, is only
    written when callers pass it (currently no caller does), and
    `lcsas location list` / `location status` never read it.
