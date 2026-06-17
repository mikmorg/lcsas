# Consolidate & Catalog Operations Workflows

This document covers the long-running maintenance workflows that keep an
LCSAS archive healthy across years (or decades) of operation.  The burn
pipeline is responsible for getting data *onto* discs; these workflows
are responsible for keeping that on-disc fleet coherent with the
catalog, reducing slot count as data is pruned, and walking volumes
through their controlled retirement.

## The maintenance lifecycle

A healthy LCSAS archive evolves through three overlapping phases:

1. **Growth.**  New snapshots add new packs.  `lcsas scan` registers
   them; `lcsas burn` packs them onto fresh volumes.  At first the
   archive grows essentially monotonically — every disc is at or near
   capacity, every pack is referenced by an unpruned snapshot.
2. **Erosion.**  Old snapshots expire, `rustic prune` removes their
   pack files from the mirror, and the catalog marks those packs
   `is_pruned=1`.  Over months and years discs become "swiss cheese":
   they still occupy a slot but only a small fraction of their bytes
   are still live data.  `consolidate` is the response — it migrates
   the surviving packs onto a smaller number of fresh discs so the
   sparsely-populated originals can be retired.
3. **Retirement.**  Each physical disc walks through a strict status
   ladder.  `STAGING → BURNING → BURNED → VERIFIED → DEPRECATED →
   DESTROYED` (the `VALID_TRANSITIONS` table in
   `src/lcsas/db/volumes.py`).  Each step is its own catalog
   transition with its own audit-trail entry in `volume_events`;
   nothing is implicit.  In a multi-copy archive, the same volume can
   be DEPRECATED at one location while still ACTIVE at another (the
   `volume_copies.status` column tracks per-location lifecycle —
   `src/lcsas/db/schema.py`).

Catalog integrity is the connective tissue.  The "holographic catalog"
design (`src/lcsas/staging/metadata.py`) means every disc carries a
full SQLite copy at burn time, so the master catalog can always be
rebuilt from any reasonably-recent disc set (`lcsas catalog rebuild`).
`lcsas catalog reconcile` reports catalog/physical-state drift (ghost
volumes that were never burned; durable volumes with no ACTIVE copy),
and periodic `lcsas catalog validate` against random discs detects
silent rot — pack files lost to media decay, content that no longer
hashes to its filename (`--content`), or catalog entries that have
drifted out of sync.

The broader system design lives in [`docs/architecture.md`](../architecture.md).

## Table of contents

1. [`lcsas consolidate` (dry-run / plan)](#lcsas-consolidate-dry-run--plan)
2. [`lcsas consolidate --execute`](#lcsas-consolidate---execute)
3. [`lcsas consolidate --deprecate`](#lcsas-consolidate---deprecate)
4. [`lcsas catalog validate`](#lcsas-catalog-validate)
5. [`lcsas catalog reconcile`](#lcsas-catalog-reconcile)
6. [`lcsas catalog rebuild`](#lcsas-catalog-rebuild)
7. [`lcsas volume impact`](#lcsas-volume-impact)
8. [Volume lifecycle state transitions](#volume-lifecycle-state-transitions)
9. [Audit trail: `volume_events`](#audit-trail-volume_events)

---

## `lcsas consolidate` (dry-run / plan)

**Purpose:**  Preview the FFD repack of one or more eroded source
volumes — show how many fresh target volumes their surviving (non-pruned)
packs will require, without touching any state.

**Prerequisites:**
- Master catalog reachable (`--db` / config `db_path`).
- Source volumes exist in the catalog and contain packs.
- Pack rows for sources have `is_pruned` correctly set by a prior
  `lcsas scan` or pack-pruning workflow (pruned packs are *excluded*
  from the consolidation plan — `tests/unit/test_consolidate.py:46-62`).

**Steps:**
1. Parse `volume_ids` (positional, one or more) and `--target-media`
   (default `MDISC100`) from the CLI (`build_parser()`).
2. Open a locked DB connection and ensure schema is current
   (`cmd_consolidate`).
3. Resolve `--target-media` to a `MediaType`; reject unknown names
   with the list of valid types (`cmd_consolidate`).
4. Construct `VolumeMerger` with the configured metadata reserve
   (default 100 MiB — `cmd_consolidate`,
   `src/lcsas/consolidate/merger.py`).
5. Call `merger.plan_consolidation(volume_ids, media_type)`
   (`cmd_consolidate`).  Internally:
   1. Validate every source volume exists; collect labels
      (`src/lcsas/consolidate/merger.py`).
   2. Pull active (non-pruned) packs from those volumes via
      `get_packs_only_on_volumes` — a `DISTINCT` join across
      `packs` ↔ `volume_packs` filtering `is_pruned = 0`
      (`src/lcsas/consolidate/merger.py`,
      `src/lcsas/db/queries.py`).
   3. Sum `size_bytes`; call `estimate_volumes_needed` with target
      capacity, the metadata reserve, and the target media's ECC
      overhead percentage
      (`src/lcsas/consolidate/merger.py`).
   4. Return a `ConsolidationPlan` dataclass with labels, active
      packs, total bytes, target media, `volumes_needed`, and any
      `pruned_left_behind` packs
      (`src/lcsas/consolidate/merger.py`).
6. Log the plan summary (source labels, pack count, total GB, target
   media, volumes needed) at INFO level.  If any packs on the source
   volumes are marked pruned, warn that they will NOT migrate (and
   hint at `lcsas pack unprune <sha256>`) (`cmd_consolidate`).
7. Without `--execute` and without `--deprecate`, print the
   next-step hint and exit 0 (`cmd_consolidate`).

**Expected outcome:**
- No catalog mutations.  No status transitions.  No staging.
- Operator sees pack count, total bytes, and target-disc count to
  decide whether the consolidation is worth running.
- Exit code 0 on success, 1 only on argument errors (bad media type
  or missing volume).

**Variant axes that apply:**
- **Media type** — `--target-media` accepts any `MediaType` enum
  member (`BD25`, `MDISC100`, `BDXL100`, `TEST_TINY` —
  `src/lcsas/config/media.py`).  Bigger targets mean fewer
  output volumes; ECC overhead is media-specific.
- **Multi-tenant** — packs from multiple repos can be in the same
  plan; `get_packs_only_on_volumes` does not filter by `repo_id`
  (`src/lcsas/db/queries.py`).  All repos' active packs
  flow through together.
- **Optical-drive count** — irrelevant for the plan-only step
  (no I/O).
- **Multi-copy** — the plan looks at *catalog* membership, not at
  which physical copies exist.  Operators with `volume_copies` at
  multiple locations should consult `volume_copies` separately
  before deprecating.
- **Recovery tier** — pure HOT-tier operation (reads catalog +
  Rustic mirror — no disc access).

**Test coverage:**
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_plan_consolidation`
  — 3 source volumes, 15 active packs, MDISC100 target.
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_pruned_packs_excluded`
  — confirms `is_pruned=1` packs are dropped from the plan.

**Source refs:**
- `cmd_consolidate` (plan path) in `src/lcsas/cli/main.py`
- `src/lcsas/consolidate/merger.py` — `ConsolidationPlan`,
  `plan_consolidation`
- `src/lcsas/db/queries.py` — `get_packs_only_on_volumes`
- `src/lcsas/binpack/algorithm.py` — `estimate_volumes_needed`

---

## `lcsas consolidate --execute`

**Purpose:**  Mark source volumes `CONSOLIDATING`, stage the surviving
packs into a fresh burn session (one or more new volumes on the target
media), and hand the operator the session ID to burn.  Does **not**
deprecate the sources — that is a separate, post-verification step.

**Prerequisites:**
- All dry-run / plan prerequisites above.
- `--config` is required (`cmd_consolidate`).
- Source volumes are in `VERIFIED` status (the only legal source for
  `CONSOLIDATING` — `VALID_TRANSITIONS` in `src/lcsas/db/volumes.py`).
- Interactive terminal (stdin is `tty`) for the irreversible-change
  confirmation prompt (`cmd_consolidate`).
- xorriso and dvdisaster runners available on `PATH` (the
  orchestrator uses `SubprocessXorrisoRunner` / `SubprocessDVDisasterRunner`
  — `cmd_consolidate`).

**Steps:**
1. The plan-path steps execute identically — the
   `ConsolidationPlan` is built first (`cmd_consolidate`).
2. `--deprecate` and `--execute` are mutually exclusive; reject both
   set (`cmd_consolidate`).
3. Confirm config is loaded; abort otherwise (`cmd_consolidate`).
4. Prompt the operator for `yes` confirmation; abort on anything
   else, error on `EOFError` (non-interactive stdin —
   `cmd_consolidate`).
5. Build a `BurnOrchestrator` with the xorriso/dvdisaster runners
   (`cmd_consolidate`).
6. Call `merger.mark_sources_consolidating(volume_ids)` to flip every
   source from `VERIFIED → CONSOLIDATING` *before* staging
   (`cmd_consolidate`, `src/lcsas/consolidate/merger.py`).  This is the
   crash-recovery marker — if the process dies mid-burn, operators can
   see intent in the catalog rather than finding `ACTIVE` volumes that
   look unrelated to any in-flight work.
7. Build a SHA-256 list from `plan.active_packs` and call
   `orch.stage(media_type, pack_sha256s=...)` (`cmd_consolidate`).
8. **On staging failure:** call `merger.abort_consolidation` to revert
   every source `CONSOLIDATING → VERIFIED`, log with `exc_info`, and
   re-raise (`cmd_consolidate`, `src/lcsas/consolidate/merger.py`).
9. **On success:** log the staged session ID and the remaining
   manual steps (`burn`, verify, `consolidate --deprecate`)
   (`cmd_consolidate`).

**Expected outcome:**
- Every source volume is in `CONSOLIDATING` status.
- A new burn session exists with one or more `STAGING` volumes
  containing exactly the source set's active packs.
- The operator gets a printed next-step list ending in
  `consolidate --deprecate <ids>`.
- Source `volume_copies` rows are untouched (deprecation happens
  later).
- Exit code 0 on success.  Non-zero on staging failure (after
  rollback to `VERIFIED`).

**Variant axes that apply:**
- **Media type** — bigger targets → fewer volumes_needed but a
  single failed disc loses more data; consider redundancy.
- **Multi-tenant** — staged volumes carry packs from any number of
  repos; the holographic injector embeds the full catalog +
  per-repo Rustic metadata on every disc
  (`src/lcsas/staging/metadata.py`).
- **Optical-drive count** — affects only the downstream `burn`
  step.
- **Multi-copy** — staging produces new volumes that need their
  own multi-copy schedule.  The pre-existing copies of the source
  volumes are not touched here.
- **Recovery tier** — HOT (catalog) + WARM (staging tree) tiers.
  No optical I/O until the next `burn`.

**Test coverage:**
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_mark_sources_consolidating`
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_abort_consolidation`
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_mark_consolidating_multiple_volumes`
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_abort_consolidation_multiple_volumes`
- **Gap:** no end-to-end CLI test of `cmd_consolidate --execute` that
  exercises the orchestrator (the confirmation prompt + subprocess
  runners are not stubbed in any test).

**Source refs:**
- `cmd_consolidate` in `src/lcsas/cli/main.py`
- `src/lcsas/consolidate/merger.py` — state-transition helpers
- `src/lcsas/burn/orchestrator.py` — `stage()`

---

## `lcsas consolidate --deprecate`

**Purpose:**  After the consolidated target volumes are burned **and
verified**, retire the original source volumes by transitioning them
`CONSOLIDATING → DEPRECATED` (or, if `--execute` was never used,
`VERIFIED → DEPRECATED`).  This is the final, safe step of the
two-phase consolidation.

**Prerequisites:**
- `--deprecate` and `--execute` are mutually exclusive
  (`cmd_consolidate`).
- For each source volume, every active pack must already exist on at
  least one other `BURNED`/`VERIFIED` volume — otherwise the
  deprecation safety check raises `ValueError`
  (`check_deprecation_safe` in `src/lcsas/db/volumes.py`).
- Sources are in `VERIFIED` or `CONSOLIDATING` — both are legal
  predecessors of `DEPRECATED`
  (`VALID_TRANSITIONS` in `src/lcsas/db/volumes.py`).

**Steps:**
1. Parse args; resolve DB path; build a `VolumeMerger`
   (`cmd_consolidate`).
2. Build the plan as in the dry-run path (the plan is constructed
   before the `--deprecate` branch — `cmd_consolidate`).
3. Enter the `--deprecate` branch (`cmd_consolidate`).
4. Call `merger.deprecate_sources(volume_ids)`
   (`src/lcsas/consolidate/merger.py`).  For each ID this calls
   `update_status(conn, vid, "DEPRECATED", commit=False)`.
   `update_status` then:
   1. Validates the transition (`VERIFIED` or `CONSOLIDATING` →
      `DEPRECATED` are both legal — `VALID_TRANSITIONS` in
      `src/lcsas/db/volumes.py`).
   2. Runs `check_deprecation_safe` inside a SAVEPOINT to find packs
      that would become unreplicated; if any exist, ROLLBACK and raise
      (`src/lcsas/db/volumes.py`).
   3. Writes the status change.
   4. Inserts a `NOTE` event into `volume_events` with detail
      `"Status changed: <prev> → DEPRECATED"`
      (`src/lcsas/db/volumes.py`).
5. After the loop, commit (`src/lcsas/consolidate/merger.py`).
6. Log the deprecated count and return 0 (`cmd_consolidate`).

**Expected outcome:**
- All source volumes are `DEPRECATED`.
- `volume_events` contains a `NOTE` row per source recording the
  transition.
- Existing `volume_copies` rows for the deprecated volumes remain
  `ACTIVE` until separately marked via
  `db.volume_copies.deprecate_copy` / `destroy_copy`
  (`src/lcsas/db/volume_copies.py`) — see the *Multi-copy*
  axis below.  The CLI surface for this is
  `lcsas copy deprecate <volume_label> <location>` /
  `lcsas copy destroy <volume_label> <location>`.
- Restore planners now de-prefer these volumes
  (`v.status NOT IN ('DEPRECATED', 'DESTROYED')` —
  `src/lcsas/db/queries.py`).
- Exit code 0 on success.  Non-zero (`ValueError` propagated) if the
  safety check refuses one of the sources.

**Variant axes that apply:**
- **Media type** — irrelevant.
- **Multi-tenant** — the safety check spans repos; a pack only on
  this volume and another repo's volume still counts as replicated.
- **Optical-drive count** — irrelevant.
- **Multi-copy** — *critical*: `update_status` changes only the
  `volumes.status` row, not `volume_copies`.  In an N-location
  archive, each physical copy needs its own
  `lcsas copy deprecate <volume_label> <location>` /
  `lcsas copy destroy <volume_label> <location>` call (which call
  `deprecate_copy` / `destroy_copy` in
  `src/lcsas/db/volume_copies.py`).  `consolidate --deprecate` does
  **not** propagate the lifecycle to `volume_copies` automatically.
- **Recovery tier** — HOT only.

**Test coverage:**
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_deprecate_sources`
- `tests/unit/test_consolidate.py::TestVolumeMerger::test_mark_consolidating_then_deprecate`
  (full VERIFIED → CONSOLIDATING → DEPRECATED happy path).
- **Gap:** no test exercises `cmd_consolidate --deprecate` directly —
  the deprecation-safety refusal at the CLI layer is untested.
- **Gap:** no test covers the multi-copy fan-out — i.e., what
  happens to `volume_copies` rows when a `DEPRECATED` volume still
  has `ACTIVE` copies at remote sites.  `consolidate --deprecate` does
  not call `deprecate_copy`/`destroy_copy` itself; the operator drives
  those via `lcsas copy deprecate`/`destroy`.

**Source refs:**
- `cmd_consolidate` (`--deprecate` branch) in `src/lcsas/cli/main.py`
- `src/lcsas/consolidate/merger.py` — `deprecate_sources`
- `src/lcsas/db/volumes.py` (status update with audit + safety;
  `check_deprecation_safe`)
- `src/lcsas/db/volume_copies.py` (per-location lifecycle)

---

## `lcsas catalog validate`

**Purpose:**  Cross-check a mounted (or extracted) LCSAS disc's
`data/` directory against the `catalog.db` embedded on the same disc.
Detects missing pack files (media decay, accidental deletion) and
orphaned pack files (catalog drift, partial restores).  With
`--content`, additionally reads every pack on disc and flags any whose
content no longer hashes to its filename (CORRUPT).

**Prerequisites:**
- Disc is mounted or extracted to a directory containing both
  `catalog.db` and a `data/` subdirectory (`src/lcsas/db/verify.py`).
- Read access to `catalog.db` (opened with `mode=ro` URI —
  `src/lcsas/db/verify.py`).

**Steps:**
1. Argparse routes `lcsas catalog validate <disc> [--content]` to
   `cmd_catalog_validate` (`build_parser()`).
2. Reject if `disc` is not a directory (`cmd_catalog_validate`).
3. Call `validate_disc(disc_path, content=args.content)`
   (`cmd_catalog_validate`).  Internally:
   1. Confirm `catalog.db` exists at the disc root; raise
      `FileNotFoundError` otherwise (`src/lcsas/db/verify.py`).
   2. Confirm `data/` exists; raise `ValueError` otherwise
      (`src/lcsas/db/verify.py`).
   3. Walk `data/**` collecting every 64-char-hex filename into a
      set (`disc_hashes`).  Handles both flat (`data/HASH`) and
      two-level (`data/ab/abcdef…`) layouts (`src/lcsas/db/verify.py`).
   4. If `volume_info.json` exists at the disc root, read its
      `sha256_manifest` for the expected set.  If the manifest is
      absent or missing, fall back to querying the embedded
      catalog: find every volume that contains any disc-present
      pack, then collect that volume's full pack list
      (`src/lcsas/db/verify.py`).
   5. Compute `catalog - disc` (missing) and `disc - catalog`
      (orphaned), sorted lexicographically.  When `content=True`,
      also read each on-disc pack and record any whose content does
      not hash to its filename in `corrupt_on_disc`
      (`src/lcsas/db/verify.py`).
4. Log per-category results (volume label, pack counts, then each
   missing/orphaned/corrupt SHA) (`cmd_catalog_validate`).
5. Return 0 if `result.ok` (all three sets empty), 1 otherwise
   (`cmd_catalog_validate`).

**Expected outcome:**
- Exit 0 + "Catalog validation PASSED" log when in sync.
- Exit 1 + per-pack `MISSING:`/`ORPHAN:`/`CORRUPT:` log lines when out
  of sync (CORRUPT only appears under `--content`).
- No mutations to the master catalog or to the disc itself.
- No automatic `volume_events` recording — if a `VERIFY_FAIL` should
  be logged, the operator must run `lcsas verify --mark-failed` as a
  follow-up.

**Variant axes that apply:**
- **Media type** — works on any disc; the two-level pack layout is
  the convention for large media (MDISC100, BDXL100).
- **Multi-tenant** — the embedded `catalog.db` contains every repo
  the disc carries; the query filters by which packs are present, so
  multi-repo discs are handled transparently.
- **Optical-drive count** — one drive at a time; the command takes a
  filesystem path, not a device — mounting/extraction is upstream.
- **Multi-copy** — validates exactly one physical copy at a time.
  Each location's copy of the same volume must be validated
  independently.
- **Recovery tier** — COLD (reads optical media) + HOT (reads its
  own embedded catalog).  Does not touch the master HOT catalog.

**Test coverage:**
- `tests/unit/test_db_verify.py::TestCollectDiscPacks` (flat +
  two-level layouts).
- Additional unit cases in `tests/unit/test_db_verify.py` cover
  `validate_disc` happy/error paths and the `--content` corruption
  detection.
- **Gap:** no test verifies that a `VERIFY_FAIL`/`VERIFY_PASS`
  event is *automatically* written to `volume_events` on
  validate — because the command does not write one.  Operators
  must couple this with `lcsas verify --mark-verified|--mark-failed`.

**Source refs:**
- `build_parser()` (argparse) in `src/lcsas/cli/main.py`
- `cmd_catalog_validate` in `src/lcsas/cli/main.py`
- `validate_disc` in `src/lcsas/db/verify.py`

---

## `lcsas catalog reconcile`

**Purpose:**  Report (and optionally repair) disagreements between the
catalog and physical reality (FMA-01).  Two independent checks:

1. **Ghost volumes** — `STAGING`/`BURNING` volumes with **zero**
   `volume_copies` rows, older than the cutoff.  These claim packs but
   correspond to no physical disc; a restore pick list would offer them
   by label and an heir would hunt for a disc that never existed.
   `--fix` deletes them and returns their packs to the unarchived pool.
2. **Durable volumes without an ACTIVE copy** — status says a disc
   exists but no copy record backs it (status/copies drift).
   Report-only; never auto-fixed.

**Prerequisites:**
- Master catalog reachable (`--db` / config `db_path`).
- For `--fix`: an interactive terminal for the confirmation prompt,
  or `--yes` to skip it (`cmd_catalog_reconcile`).

**Steps:**
1. Argparse routes `lcsas catalog reconcile [--fix] [--yes]
   [--older-than-hours N]` to `cmd_catalog_reconcile`
   (`build_parser()`).  `--older-than-hours` defaults to **24**.
2. Open a locked DB connection and ensure schema is current
   (`cmd_catalog_reconcile`).
3. Query `get_durable_volumes_without_active_copies` and warn for each
   drifted volume (report-only) (`src/lcsas/db/queries.py`).
4. Query `get_ghost_volumes(conn, older_than_hours)`; if none, log the
   all-clear and return 0 (`src/lcsas/db/queries.py`).
5. For each ghost, tally per-repo pack/byte stats via
   `get_volume_pack_stats_by_repo` and warn
   (`src/lcsas/db/queries.py`).
6. Without `--fix`: print the `--fix` hint and return **1** (ghosts
   are a non-clean state).
7. With `--fix`: unless `--yes` is given, prompt for `yes`
   confirmation (abort on `EOFError` or any other answer); then
   `delete_volume` each ghost and report the reclaimed pack count
   (`src/lcsas/db/volumes.py`).

**Expected outcome:**
- Report-only run: exit 0 if no ghosts; exit 1 if any ghost exists
  (drifted-but-durable volumes are warnings, not a failure on their
  own).
- `--fix` run: ghost volumes deleted, their packs returned to the
  unarchived pool (re-selectable by a later `lcsas stage`).
- The drift check (durable-without-ACTIVE-copy) is never auto-fixed —
  it always requires operator investigation.

**Variant axes that apply:**
- **Multi-tenant** — ghost pack stats are reported per repo.
- **Multi-copy** — the durable-without-ACTIVE-copy check is exactly
  the multi-copy drift detector: a volume whose `volume_copies` rows
  are all DEPRECATED/DESTROYED (or absent) despite a durable status.
- **Recovery tier** — HOT only (catalog-only).

**Source refs:**
- `build_parser()` (argparse) in `src/lcsas/cli/main.py`
- `cmd_catalog_reconcile` in `src/lcsas/cli/main.py`
- `get_ghost_volumes`, `get_durable_volumes_without_active_copies`,
  `get_volume_pack_stats_by_repo` in `src/lcsas/db/queries.py`

---

## `lcsas catalog rebuild`

**Purpose:**  Reconstruct the master catalog from one or more mounted
discs' embedded holographic catalogs.  This is the disaster-recovery
path when the HOT-tier master DB is lost or corrupted — every disc
carries a self-describing SQLite snapshot, so re-merging any
sufficiently recent set of them restores the master.

`--output` is **required**.

**Prerequisites:**
- One or more disc directories mounted/extracted; each contains a
  readable `catalog.db` at its root (`src/lcsas/db/rebuild.py`).
- Write access to the `--output` DB path (created if missing, or
  merged into if it already exists — `src/lcsas/db/rebuild.py`).
- SQLite ≥ 3.33.0 is *not* strictly required (the implementation
  uses explicit loops instead of `UPDATE...FROM` —
  `src/lcsas/db/rebuild.py`).

**Steps:**
1. Argparse routes `lcsas catalog rebuild <disc_dirs…> --output PATH`
   to `cmd_catalog_rebuild` (`build_parser()`).
2. Sanity-check that every supplied path is a directory; error if
   any is not (`cmd_catalog_rebuild`).
3. Call `rebuild_catalog(disc_dirs, output_db)`
   (`cmd_catalog_rebuild`).  Internally:
   1. Open (or create) `output_db` and run `ensure_schema` to install
      the current (v9) schema.  Pre-existing master rows seed a
      per-volume freshness baseline from their `created_at`
      (`src/lcsas/db/rebuild.py`).
   2. **Sort discs newest-first** by the source catalog's freshness
      (FMA-06).  Discs lacking `catalog.db` sort last and only emit a
      skip error (`src/lcsas/db/rebuild.py`).
   3. For each disc (newest first):
      1. Skip discs lacking `catalog.db`; record the skip in
         `RebuildResult.errors` and bump `discs_skipped`.
      2. Call `_merge_one_disc(...)` (`src/lcsas/db/rebuild.py`).  This
         ATTACHes the source DB and runs INSERT-OR-IGNORE passes over
         `repositories` (keyed on `repo_id`), `locations` (`name`),
         `packs` (`sha256`), `volumes` (`uuid`), `snapshots`
         (`snapshot_id`), `volume_packs` and `volume_copies` (both
         **ID-translated** via `uuid`/`sha256` joins because
         auto-increment IDs differ between DBs), plus the audit tables
         `volume_events`, `burn_sessions`, and `session_volumes`.
         Because discs are merged newest-first, INSERT-OR-IGNORE keeps
         the freshest catalog's row for each natural key.
      3. **Recency-aware volume-status resolution (FMA-06).**  For
         every volume the disc shares with the target, the *freshest*
         catalog that mentions it owns its status — **including
         downgrades to DEPRECATED/DESTROYED**.  A `_STATUS_RANK` ladder
         (`VERIFIED > BURNED > CONSOLIDATING > BURNING > STAGING >
         DEPRECATED > DESTROYED`) is used **only to detect the
         resurrection hazard**: if a staler disc claims a volume is
         *more* alive than the freshest record, the fresh status is
         kept and a warning is appended to `RebuildResult.warnings`.
         A destroyed volume is therefore **never resurrected** by an
         older disc that predates its destruction.
      4. Tally per-table insert counts.
   4. Append a summary warning if staler discs disagreed on
      `is_pruned`, then return a `RebuildResult`
      (`src/lcsas/db/rebuild.py`).
4. Log a per-table summary (discs processed/skipped; repos, volumes,
   packs, snapshots merged) and any `RebuildResult.warnings`
   (`cmd_catalog_rebuild`).
5. Log each error if any occurred; return 1 in that case, 0
   otherwise (`cmd_catalog_rebuild`).

**Expected outcome:**
- A populated SQLite catalog at `--output` containing every
  natural-key-unique repo, volume, pack, snapshot, `volume_pack`,
  and `volume_copy` from every successfully-merged disc.
- Volume statuses converge to the **freshest** observed view, not the
  most-alive one — downgrades stick and destroyed volumes are never
  resurrected (FMA-06).  Resurrection-hazard disagreements surface as
  `RebuildResult.warnings`.
- Exit 0 if all discs merged; 1 if any disc was skipped or errored
  (master DB still contains successfully-merged data).

**Variant axes that apply:**
- **Media type** — irrelevant.
- **Multi-tenant** — repos are merged on `repo_id`; multi-tenant
  archives reconstruct cleanly even when individual discs carry
  only a subset of the repos.
- **Optical-drive count** — affects only how quickly operators can
  mount discs sequentially (or in parallel with multiple drives).
  The rebuild itself is single-threaded
  (`src/lcsas/db/rebuild.py`).
- **Multi-copy** — `volume_copies` is keyed on `(volume_id,
  location)`, so merging discs from multiple sites coalesces all
  known copies of each volume.
- **Recovery tier** — COLD → HOT.  The output is a fresh HOT-tier
  catalog usable to drive future scan/burn/restore.

**Test coverage:**
- `tests/unit/test_db_rebuild.py::TestRebuildMerge::test_merge_simple_volumes`
- `…::test_merge_status_conflict_prefers_higher_quality`
  (BURNED ← VERIFIED upgrade).
- `…::test_merge_status_conflict_keeps_better_status`
  (VERIFIED is not downgraded to BURNED).
- `…::test_merge_packs_deduplicates_by_sha256`
- `…::test_rebuild_catalog_skip_missing_disc`
- `…::test_rebuild_catalog_processes_multiple_discs`
- `…::test_rebuild_handles_corrupt_source` (truncated SQLite file).
- `…::test_merge_snapshots`
- `…::test_merge_volume_packs_with_id_translation`
- `…::test_merge_volume_copies_preserves_all_fields`
- The `DESTROYED → VERIFIED` resurrection hazard — an older disc that
  predates a volume's destruction — is handled by the recency-aware
  merge (FMA-06): the fresh DESTROYED status is kept and a warning is
  emitted.  This replaces the old liveness-rank behavior that could
  silently resurrect destroyed records.
- **Gap:** no test covers merging when the target DB already
  contains rows the source disagrees with on *non-status* columns
  (e.g. `repositories.name` drift) — INSERT OR IGNORE silently
  retains the existing (newest-first) value.
- **Gap:** no test exercises the CLI handler `cmd_catalog_rebuild`
  end-to-end (only the underlying `rebuild_catalog` /
  `_merge_one_disc` API).

**Source refs:**
- `build_parser()` (argparse) in `src/lcsas/cli/main.py`
- `cmd_catalog_rebuild` in `src/lcsas/cli/main.py`
- `rebuild_catalog` / `_merge_one_disc` in `src/lcsas/db/rebuild.py`
- `ensure_schema` in `src/lcsas/db/schema.py`

---

## `lcsas volume impact`

**Purpose:**  Blast-radius report for a single disc (FMA-08): if
**every** copy of this volume failed, what becomes unrestorable?
Answers "is it safe to lose this disc?" before retiring or destroying
it.

**Prerequisites:**
- Master catalog reachable (`--db` / config `db_path`).
- The volume exists in the catalog (looked up by **label**, not ID).
- `--snapshots` additionally needs the live Rustic mirror reachable
  to map at-risk packs to the snapshots they break.

**Steps:**
1. Argparse routes `lcsas volume impact <volume_label> [--snapshots]`
   to `cmd_volume_impact` (`build_parser()`).
2. Resolve the volume by label; error 1 if not found
   (`cmd_volume_impact`).
3. List all `volume_copies` (active and not); warn if no ACTIVE copy
   is recorded (the disc may already be lost).  Print each copy's
   location, status, and last-verified age (`cmd_volume_impact`).
4. Compute the at-risk packs — packs on this volume with **no other
   live copy** — via `get_at_risk_packs_for_volume`
   (`src/lcsas/db/queries.py`).
5. If nothing is at risk, log "Blast radius: NONE" and return 0.
   Otherwise warn with the total pack count / GB, broken down per
   repo (`cmd_volume_impact`).
6. With `--snapshots`, additionally print which snapshots each
   repo's at-risk packs would break (`_print_snapshot_impact`).

**Expected outcome:**
- Pack-level (catalog-only) report always prints; exit 0.
- "Blast radius: NONE" when every pack has another live copy.
- Otherwise a per-repo at-risk pack/byte report, plus the
  snapshot-level view when `--snapshots` is given and the mirror is
  reachable.
- No catalog mutations.

**Variant axes that apply:**
- **Multi-tenant** — at-risk packs are grouped and reported per repo.
- **Multi-copy** — *central to this command*: "at risk" means no
  **other** ACTIVE copy of the pack exists anywhere; redundant copies
  shrink the blast radius to zero.
- **Recovery tier** — HOT (catalog) always; HOT mirror also read when
  `--snapshots` is requested.

**Source refs:**
- `build_parser()` (argparse) in `src/lcsas/cli/main.py`
- `cmd_volume_impact` in `src/lcsas/cli/main.py`
- `get_at_risk_packs_for_volume` in `src/lcsas/db/queries.py`

See also `lcsas status --redundancy` for fleet-wide blast-radius
reporting (FMA-08).

---

## Volume lifecycle state transitions

LCSAS encodes the disc lifecycle as a finite-state machine, enforced
at the catalog layer.  The full transition table is the
`VALID_TRANSITIONS` dict in `src/lcsas/db/volumes.py`:

```text
STAGING       → BURNING, DEPRECATED, DESTROYED
BURNING       → BURNED, VERIFIED, STAGING, DESTROYED
BURNED        → BURNING, VERIFIED, STAGING, DESTROYED
VERIFIED      → DEPRECATED, DESTROYED, CONSOLIDATING
CONSOLIDATING → DEPRECATED, VERIFIED
DEPRECATED    → DESTROYED
DESTROYED     → (terminal)
```

(`BURNED → BURNING` is the re-burn retry after a failed post-burn
verify.)  The CHECK constraint in the schema (currently v9) enumerates
the legal statuses (`src/lcsas/db/schema.py`).  The `CONSOLIDATING`
status was added by the v4→v5 migration in the same module.

**Which commands trigger each transition (for this category):**

| From → To                      | Trigger                                            | Source                                          |
| ------------------------------ | -------------------------------------------------- | ----------------------------------------------- |
| `STAGING → BURNING → VERIFIED` | `catalog import-receipts` w/ `verify_passed=true` (offline-burn receipt) | `cmd_catalog_import`   |
| `STAGING → BURNING → BURNED`   | `catalog import-receipts` w/ `verify_passed=false` | `cmd_catalog_import`                            |
| `BURNED → VERIFIED`            | `lcsas verify --mark-verified`                     | `cmd_verify` in `src/lcsas/cli/main.py`         |
| `VERIFIED → CONSOLIDATING`     | `lcsas consolidate --execute` (just before staging) | `cmd_consolidate`, `src/lcsas/consolidate/merger.py` |
| `CONSOLIDATING → VERIFIED`     | Staging failure during `--execute` (auto-rollback) | `cmd_consolidate`, `src/lcsas/consolidate/merger.py` |
| `CONSOLIDATING → DEPRECATED`   | `lcsas consolidate --deprecate` (post-verification) | `cmd_consolidate`, `src/lcsas/consolidate/merger.py` |
| `VERIFIED → DEPRECATED`        | `lcsas consolidate --deprecate` (no `--execute` first) | Same as above (also legal per `VALID_TRANSITIONS`). |
| `DEPRECATED → DESTROYED`       | No first-class CLI command yet — see *Gaps*       | `VALID_TRANSITIONS` in `src/lcsas/db/volumes.py` |

`update_status` itself enforces the table and, on the
`* → DEPRECATED` edge, additionally runs `check_deprecation_safe`
inside a SAVEPOINT to refuse the transition if any pack would become
unreplicated (`src/lcsas/db/volumes.py`).  Pass `force=True` to
override (logged at WARNING — `src/lcsas/db/volumes.py`).

**Per-location lifecycle (`volume_copies`):** independently from the
volume-wide status above, each physical copy has its own status of
`ACTIVE`/`DEPRECATED`/`DESTROYED` (`src/lcsas/db/schema.py`).  Helpers
`deprecate_copy(conn, volume_id, location)` and
`destroy_copy(conn, volume_id, location)` toggle these
(`src/lcsas/db/volume_copies.py`), surfaced by the CLI as
`lcsas copy deprecate <volume_label> <location>` /
`lcsas copy destroy <volume_label> <location>`.  *The deprecation
triggered by `consolidate --deprecate` does NOT cascade into
`volume_copies`* — operators with multi-copy archives must run
`lcsas copy deprecate`/`destroy` per location separately.

**Gaps for this section:**
- No first-class `lcsas destroy <volume>` command exists for the
  *volume-wide* `DEPRECATED → DESTROYED` edge; it is only reachable by
  directly calling `update_status` (e.g. from a script).  (Per-copy
  destruction is recorded via `lcsas copy destroy`.)
- `consolidate --deprecate` does not propagate a volume-level
  DEPRECATED status to the matching `volume_copies` rows.
- No test asserts that a forced `DESTROYED → *` (terminal-state)
  transition is rejected by `update_status` (it should be — empty
  transition set in `VALID_TRANSITIONS`, `src/lcsas/db/volumes.py`).

---

## Audit trail: `volume_events`

Every consolidate/deprecate transition leaves a row in
`volume_events` (`src/lcsas/db/schema.py`).  The table
enforces a CHECK on `event_type`:
`VERIFY_PASS`, `VERIFY_FAIL`, `VERIFY_FAIL_REBURN`, `ECC_REPAIR`,
`LOCATION_MOVE`, `CONDITION_CHECK`, `NOTE`, `BURN_RECEIPT_IMPORTED`
(`src/lcsas/db/schema.py`).  These constants are mirrored in the
Python layer as `VALID_EVENT_TYPES` (`src/lcsas/db/volume_events.py`).

**Which workflow writes which event:**

| Workflow                                      | Event                          | Detail field                                |
| --------------------------------------------- | ------------------------------ | ------------------------------------------- |
| `consolidate --execute`                       | `NOTE` (per source volume)     | `"Status changed: VERIFIED → CONSOLIDATING"` |
| Staging-failure auto-rollback                 | `NOTE` (per source volume)     | `"Status changed: CONSOLIDATING → VERIFIED"` |
| `consolidate --deprecate` (success)           | `NOTE` (per source volume)     | `"Status changed: <prev> → DEPRECATED"`     |
| `catalog import-receipts` (verify pass/fail)  | `VERIFY_PASS` / `VERIFY_FAIL` + `BURN_RECEIPT_IMPORTED` | receipt filename; JSON provenance (iso_sha256, session_id, device, pack_ids) |
| `catalog validate` (PASS/FAIL)                | **none** — see *Gaps*          | n/a                                         |
| `catalog rebuild`                             | merges `volume_events` rows from the disc catalogs (no synthetic "rebuilt" event) | n/a |
| `loc move` (`cmd_location`, out of category)  | `LOCATION_MOVE` (record-keeping precedent) | `<free text>`                  |

Events are inserted by `add_event` (`src/lcsas/db/volume_events.py`)
and `update_status` (`src/lcsas/db/volumes.py`).  Queries:
`get_events_for_volume`, `get_latest_event`, `get_events_by_type`
(`src/lcsas/db/volume_events.py`).

**Gaps:**
- `lcsas catalog validate` does **not** automatically record a
  `VERIFY_PASS`/`VERIFY_FAIL` against the validated volume.  An
  operator running it as a periodic media-rot check must follow up
  with `lcsas verify --mark-verified`/`--mark-failed` to leave an
  audit trail.
- `lcsas catalog rebuild` does not record a synthetic event marking
  "catalog reconstructed from disc set" — there is no first-class
  way to query when the master DB was last rebuilt other than its
  filesystem mtime.

**Source refs:**
- `src/lcsas/db/schema.py` — `volume_events` DDL
- `src/lcsas/db/volume_events.py` — CRUD + `VALID_EVENT_TYPES`
- `src/lcsas/db/volumes.py` — automatic `NOTE` insertion on
  status transitions