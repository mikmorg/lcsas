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
   ladder.  `BURNED → VERIFIED → DEPRECATED → DESTROYED` (see
   `src/lcsas/db/volumes.py:25-33`).  Each step is its own catalog
   transition with its own audit-trail entry in `volume_events`;
   nothing is implicit.  In a multi-copy archive, the same volume can
   be DEPRECATED at one location while still ACTIVE at another (the
   `volume_copies.status` column tracks per-location lifecycle —
   `src/lcsas/db/schema.py:93-109`).

Catalog integrity is the connective tissue.  The "holographic catalog"
design (`src/lcsas/staging/metadata.py`) means every disc carries a
full SQLite copy at burn time, so the master catalog can always be
rebuilt from any reasonably-recent disc set (`lcsas catalog rebuild`).
Periodic `lcsas catalog validate` against random discs detects
silent rot — pack files lost to media decay, or catalog entries that
have drifted out of sync.

## Table of contents

1. [`lcsas consolidate` (dry-run / plan)](#lcsas-consolidate-dry-run--plan)
2. [`lcsas consolidate --execute`](#lcsas-consolidate---execute)
3. [`lcsas consolidate --deprecate`](#lcsas-consolidate---deprecate)
4. [`lcsas catalog validate`](#lcsas-catalog-validate)
5. [`lcsas catalog rebuild`](#lcsas-catalog-rebuild)
6. [Volume lifecycle state transitions](#volume-lifecycle-state-transitions)
7. [Audit trail: `volume_events`](#audit-trail-volume_events)

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
   (default `MDISC100`) from the CLI
   (`src/lcsas/cli/main.py:325-334`).
2. Open a locked DB connection and ensure schema is current
   (`src/lcsas/cli/main.py:1407-1410`).
3. Resolve `--target-media` to a `MediaType`; reject unknown names
   with the list of valid types
   (`src/lcsas/cli/main.py:1412-1418`).
4. Construct `VolumeMerger` with the configured metadata reserve
   (default 100 MiB — `src/lcsas/cli/main.py:1420-1421`,
   `src/lcsas/consolidate/merger.py:34-40`).
5. Call `merger.plan_consolidation(volume_ids, media_type)`
   (`src/lcsas/cli/main.py:1422`).  Internally:
   1. Validate every source volume exists; collect labels
      (`src/lcsas/consolidate/merger.py:57-60`).
   2. Pull active (non-pruned) packs from those volumes via
      `get_packs_only_on_volumes` — a `DISTINCT` join across
      `packs` ↔ `volume_packs` filtering `is_pruned = 0`
      (`src/lcsas/consolidate/merger.py:63`,
      `src/lcsas/db/queries.py:366-394`).
   3. Sum `size_bytes`; call `estimate_volumes_needed` with target
      capacity, the metadata reserve, and the target media's ECC
      overhead percentage
      (`src/lcsas/consolidate/merger.py:64-73`).
   4. Return a `ConsolidationPlan` dataclass with labels, active
      packs, total bytes, target media, and `volumes_needed`
      (`src/lcsas/consolidate/merger.py:75-82`).
6. Log the plan summary (source labels, pack count, total GB, target
   media, volumes needed) at INFO level
   (`src/lcsas/cli/main.py:1424-1429`).
7. Without `--execute` and without `--deprecate`, print the
   next-step hint and exit 0
   (`src/lcsas/cli/main.py:1441-1446`).

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
  (`src/lcsas/db/queries.py:366-394`).  All repos' active packs
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
- `src/lcsas/cli/main.py:1399-1446` — `cmd_consolidate` plan path
- `src/lcsas/consolidate/merger.py:14-82` — `ConsolidationPlan`,
  `plan_consolidation`
- `src/lcsas/db/queries.py:366-394` — `get_packs_only_on_volumes`
- `src/lcsas/binpack/algorithm.py` — `estimate_volumes_needed`

---

## `lcsas consolidate --execute`

**Purpose:**  Mark source volumes `CONSOLIDATING`, stage the surviving
packs into a fresh burn session (one or more new volumes on the target
media), and hand the operator the session ID to burn.  Does **not**
deprecate the sources — that is a separate, post-verification step.

**Prerequisites:**
- All `--dry-run` prerequisites above.
- `--config` is required (`src/lcsas/cli/main.py:1448-1450`).
- Source volumes are in `VERIFIED` status (the only legal source for
  `CONSOLIDATING` — `src/lcsas/db/volumes.py:29-30`).
- Interactive terminal (stdin is `tty`) for the irreversible-change
  confirmation prompt (`src/lcsas/cli/main.py:1458-1466`).
- xorriso and dvdisaster runners available on `PATH` (the
  orchestrator uses `SubprocessXorrisoRunner` / `SubprocessDVDisasterRunner`
  — `src/lcsas/cli/main.py:1469-1481`).

**Steps:**
1. Steps 1-6 of the dry-run path execute identically — the
   `ConsolidationPlan` is built first
   (`src/lcsas/cli/main.py:1422`).
2. `--deprecate` and `--execute` are mutually exclusive; reject both
   set (`src/lcsas/cli/main.py:1432-1435`).
3. Confirm config is loaded; abort otherwise
   (`src/lcsas/cli/main.py:1448-1450`).
4. Prompt the operator for `yes` confirmation; abort on anything
   else, error on `EOFError` (non-interactive stdin —
   `src/lcsas/cli/main.py:1452-1466`).
5. Install a `ShutdownManager` and build a `BurnOrchestrator` with
   the xorriso/dvdisaster runners
   (`src/lcsas/cli/main.py:1469-1481`).
6. Call `merger.mark_sources_consolidating(volume_ids)` to flip every
   source from `VERIFIED → CONSOLIDATING` *before* staging
   (`src/lcsas/cli/main.py:1483-1485`,
   `src/lcsas/consolidate/merger.py:99-113`).  This is the crash-
   recovery marker — if the process dies mid-burn, operators can see
   intent in the catalog rather than finding `ACTIVE` volumes that
   look unrelated to any in-flight work.
7. Build a SHA-256 list from `plan.active_packs` and call
   `orch.stage(media_type, pack_sha256s=...)`
   (`src/lcsas/cli/main.py:1487-1493`).
8. **On staging failure:** call `merger.abort_consolidation` to revert
   every source `CONSOLIDATING → VERIFIED`, log with `exc_info`, and
   re-raise (`src/lcsas/cli/main.py:1494-1498`,
   `src/lcsas/consolidate/merger.py:115-122`).
9. **On success:** log the staged session ID and the three remaining
   manual steps (`burn`, verify, `consolidate --deprecate`)
   (`src/lcsas/cli/main.py:1500-1508`).

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
- `src/lcsas/cli/main.py:1399-1510` — `cmd_consolidate`
- `src/lcsas/consolidate/merger.py:84-122` — state-transition helpers
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
  (`src/lcsas/cli/main.py:1432-1435`).
- For each source volume, every active pack must already exist on at
  least one other `BURNED`/`VERIFIED` volume — otherwise the
  deprecation safety check raises `ValueError`
  (`src/lcsas/db/volumes.py:128-159`,
  `src/lcsas/db/volumes.py:246-270`).
- Sources are in `VERIFIED` or `CONSOLIDATING` — both are legal
  predecessors of `DEPRECATED`
  (`src/lcsas/db/volumes.py:29-31`).

**Steps:**
1. Parse args; resolve DB path; build a `VolumeMerger`
   (`src/lcsas/cli/main.py:1407-1421`).
2. Build the plan as in the dry-run path (no-op for `--deprecate`,
   but the plan is constructed before the branch —
   `src/lcsas/cli/main.py:1422-1429`).
3. Enter the `--deprecate` branch
   (`src/lcsas/cli/main.py:1431-1439`).
4. Call `merger.deprecate_sources(volume_ids)`
   (`src/lcsas/consolidate/merger.py:84-97`).  For each ID this calls
   `update_status(conn, vid, "DEPRECATED", commit=False)`
   (`src/lcsas/consolidate/merger.py:95-96`).  `update_status`
   then:
   1. Validates the transition (`VERIFIED` or `CONSOLIDATING` →
      `DEPRECATED` are both legal —
      `src/lcsas/db/volumes.py:25-33`).
   2. Runs `check_deprecation_safe` inside a SAVEPOINT to find packs
      that would become unreplicated; if any exist, ROLLBACK and raise
      (`src/lcsas/db/volumes.py:128-140`,
      `src/lcsas/db/volumes.py:246-270`).
   3. Writes the status change.
   4. Inserts a `NOTE` event into `volume_events` with detail
      `"Status changed: <prev> → DEPRECATED"`
      (`src/lcsas/db/volumes.py:146-153`).
5. After the loop, commit
   (`src/lcsas/consolidate/merger.py:97`).
6. Log the deprecated count and return 0
   (`src/lcsas/cli/main.py:1437-1439`).

**Expected outcome:**
- All source volumes are `DEPRECATED`.
- `volume_events` contains a `NOTE` row per source recording the
  transition.
- Existing `volume_copies` rows for the deprecated volumes remain
  `ACTIVE` until separately marked via
  `db.volume_copies.deprecate_copy` / `destroy_copy`
  (`src/lcsas/db/volume_copies.py:158-184`) — see the *Multi-copy*
  axis below.
- Restore planners now de-prefer these volumes
  (`v.status NOT IN ('DEPRECATED', 'DESTROYED')` — e.g.
  `src/lcsas/db/queries.py:308-410`).
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
  `deprecate_copy(volume_id, location)` /
  `destroy_copy(volume_id, location)` call
  (`src/lcsas/db/volume_copies.py:158-184`).  The current CLI does
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
  has `ACTIVE` copies at remote sites.  The CLI does not call
  `deprecate_copy`/`destroy_copy` itself.

**Source refs:**
- `src/lcsas/cli/main.py:1431-1439`
- `src/lcsas/consolidate/merger.py:84-97`
- `src/lcsas/db/volumes.py:105-190` (status update with audit + safety)
- `src/lcsas/db/volumes.py:246-270` (`check_deprecation_safe`)
- `src/lcsas/db/volume_copies.py:158-184` (per-location lifecycle)

---

## `lcsas catalog validate`

**Purpose:**  Cross-check a mounted (or extracted) LCSAS disc's
`data/` directory against the `catalog.db` embedded on the same disc.
Detects missing pack files (media decay, accidental deletion) and
orphaned pack files (catalog drift, partial restores).

**Prerequisites:**
- Disc is mounted or extracted to a directory containing both
  `catalog.db` and a `data/` subdirectory
  (`src/lcsas/db/verify.py:80-93`).
- Read access to `catalog.db` (opened with `mode=ro` URI —
  `src/lcsas/db/verify.py:103`).

**Steps:**
1. Argparse routes `lcsas catalog validate <disc>` to
   `cmd_catalog_validate` (`src/lcsas/cli/main.py:217-225`,
   `src/lcsas/cli/main.py:2702-2704`).
2. Reject if `disc` is not a directory
   (`src/lcsas/cli/main.py:1318-1321`).
3. Call `validate_disc(disc_path)`
   (`src/lcsas/cli/main.py:1324-1328`).  Internally:
   1. Confirm `catalog.db` exists at the disc root; raise
      `FileNotFoundError` otherwise
      (`src/lcsas/db/verify.py:81-86`).
   2. Confirm `data/` exists; raise `ValueError` otherwise
      (`src/lcsas/db/verify.py:88-93`).
   3. Walk `data/**` collecting every 64-char-hex filename into a
      set (`disc_hashes`).  Handles both flat (`data/HASH`) and
      two-level (`data/ab/abcdef…`) layouts
      (`src/lcsas/db/verify.py:39-60`).
   4. If `volume_info.json` exists at the disc root, read its
      `sha256_manifest` for the expected set
      (`src/lcsas/db/verify.py:108-122`).  If the manifest is
      absent or missing, fall back to querying the embedded
      catalog: find every volume that contains any disc-present
      pack, then collect that volume's full pack list
      (`src/lcsas/db/verify.py:126-160`).
   5. Compute `catalog - disc` (missing) and `disc - catalog`
      (orphaned), sorted lexicographically
      (`src/lcsas/db/verify.py:177-179`).
4. Log per-category results (volume label, pack counts, then each
   missing/orphaned SHA) (`src/lcsas/cli/main.py:1330-1348`).
5. Return 0 if `result.ok` (both sets empty), 1 otherwise
   (`src/lcsas/cli/main.py:1350-1359`).

**Expected outcome:**
- Exit 0 + "Catalog validation PASSED" log when in sync.
- Exit 1 + per-pack `MISSING:`/`ORPHAN:` log lines when out of sync.
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
- Additional unit cases exist in `tests/unit/test_db_verify.py`
  (full file, 239 lines, covers `validate_disc` happy/error paths).
- **Gap:** no test verifies that a `VERIFY_FAIL`/`VERIFY_PASS`
  event is *automatically* written to `volume_events` on
  validate — because the command does not write one.  Operators
  must couple this with `lcsas verify --mark-verified|--mark-failed`.

**Source refs:**
- `src/lcsas/cli/main.py:217-225` (argparse)
- `src/lcsas/cli/main.py:1314-1359` (`cmd_catalog_validate`)
- `src/lcsas/db/verify.py:13-181`

---

## `lcsas catalog rebuild`

**Purpose:**  Reconstruct the master catalog from one or more mounted
discs' embedded holographic catalogs.  This is the disaster-recovery
path when the HOT-tier master DB is lost or corrupted — every disc
carries a self-describing SQLite snapshot, so re-merging any
sufficiently recent set of them restores the master.

**Prerequisites:**
- One or more disc directories mounted/extracted; each contains a
  readable `catalog.db` at its root
  (`src/lcsas/db/rebuild.py:236-244`).
- Write access to the output DB path (created if missing —
  `src/lcsas/db/rebuild.py:231-234`).
- SQLite ≥ 3.33.0 is *not* strictly required (the implementation
  carefully uses a loop instead of `UPDATE...FROM` —
  `src/lcsas/db/rebuild.py:13-23`, `:115-150`).

**Steps:**
1. Argparse routes `lcsas catalog rebuild <disc_dirs…> --output PATH`
   to `cmd_catalog_rebuild` (`src/lcsas/cli/main.py:227-242`,
   `src/lcsas/cli/main.py:2704-2706`).
2. Sanity-check that every supplied path is a directory; error if
   any is not (`src/lcsas/cli/main.py:1366-1374`).
3. Call `rebuild_catalog(disc_dirs, output_db)`
   (`src/lcsas/cli/main.py:1379`).  Internally:
   1. Open (or create) `output_db` and run `schema.create_all` to
      install the v5 schema (`src/lcsas/db/rebuild.py:227-234`).
   2. For each disc:
      1. Skip discs lacking `catalog.db`; record the skip in
         `RebuildResult.errors`
         (`src/lcsas/db/rebuild.py:236-244`).
      2. Call `_merge_one_disc(target_conn, catalog.db)`
         (`src/lcsas/db/rebuild.py:246-253`).  This ATTACHes the
         source DB as alias `src` and runs seven INSERT-OR-IGNORE
         passes:
         - `repositories` keyed on `repo_id`
           (`src/lcsas/db/rebuild.py:62-72`).
         - `locations` keyed on `name`
           (`src/lcsas/db/rebuild.py:74-82`).
         - `packs` keyed on `sha256`
           (`src/lcsas/db/rebuild.py:84-93`).
         - `volumes` keyed on `uuid`
           (`src/lcsas/db/rebuild.py:95-107`).
         - **Volume-status conflict resolution.**  For every shared
           volume, compare statuses on a quality ladder
           (`VERIFIED=6 > BURNED=5 > CONSOLIDATING=4 > BURNING=3 >
           STAGING=2 > DEPRECATED=1 > DESTROYED=0`) and update the
           target only if the source is strictly better
           (`src/lcsas/db/rebuild.py:109-150`).  Implemented as an
           explicit loop, not `UPDATE…FROM`, for SQLite < 3.33
           compatibility (`src/lcsas/db/rebuild.py:115-116`).
         - `snapshots` keyed on `snapshot_id`
           (`src/lcsas/db/rebuild.py:152-163`).
         - `volume_packs` with **ID translation** via
           `uuid`/`sha256` joins — auto-incremented IDs differ
           between DBs (`src/lcsas/db/rebuild.py:165-179`).
         - `volume_copies` keyed on `(volume_id, location)` — also
           ID-translated (`src/lcsas/db/rebuild.py:181-195`).
         - DETACH the source DB (`src/lcsas/db/rebuild.py:199-200`).
      3. Tally per-table insert counts
         (`src/lcsas/db/rebuild.py:255-268`).
   3. Return a `RebuildResult`
      (`src/lcsas/db/rebuild.py:26-41`, `:270-271`).
4. Log a per-table summary (discs processed/skipped; repos, volumes,
   packs, snapshots merged) (`src/lcsas/cli/main.py:1381-1387`).
5. Log each error if any occurred; return 1 in that case, 0
   otherwise (`src/lcsas/cli/main.py:1389-1395`).

**Expected outcome:**
- A populated SQLite catalog at `--output` containing every
  natural-key-unique repo, volume, pack, snapshot, `volume_pack`,
  and `volume_copy` from every successfully-merged disc.
- Volume statuses converge to the highest-quality observed across
  all sources.
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
  (`src/lcsas/db/rebuild.py:236-260`).
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
- **Gap:** no test exercises a *schema-version mismatch* — a disc
  written under schema v4 (no `CONSOLIDATING` status) merged into a
  v5 master, or vice versa.  The status-quality ladder maps
  unknown statuses to 0
  (`src/lcsas/db/rebuild.py:130-143`), which means an exotic
  forward-compatible status would always lose the conflict
  resolution — undocumented behavior.
- **Gap:** no test covers merging when the target DB already
  contains rows the source disagrees with (e.g. `repositories.name`
  drift) — INSERT OR IGNORE silently retains the older value.
- **Gap:** no test exercises the CLI handler `cmd_catalog_rebuild`
  end-to-end (only the underlying `rebuild_catalog` /
  `_merge_one_disc` API).
- **Gap:** no test for the `DESTROYED → VERIFIED` resurrection case
  (which the quality ladder allows — a disc still in someone's hand
  can resurrect a record marked destroyed in the master).  This is
  intentional per the ladder but undocumented.

**Source refs:**
- `src/lcsas/cli/main.py:227-242` (argparse)
- `src/lcsas/cli/main.py:1362-1396` (`cmd_catalog_rebuild`)
- `src/lcsas/db/rebuild.py:1-271`
- `src/lcsas/db/schema.py:170-197` (`create_all`)

---

## Volume lifecycle state transitions

LCSAS encodes the disc lifecycle as a finite-state machine, enforced
at the catalog layer.  The full transition table lives in
`src/lcsas/db/volumes.py:25-33`:

```text
STAGING       → BURNING, DEPRECATED, DESTROYED
BURNING       → BURNED, VERIFIED, STAGING, DESTROYED
BURNED        → VERIFIED, STAGING, DESTROYED
VERIFIED      → DEPRECATED, DESTROYED, CONSOLIDATING
CONSOLIDATING → DEPRECATED, VERIFIED
DEPRECATED    → DESTROYED
DESTROYED     → (terminal)
```

The CHECK constraint in the schema enumerates the legal statuses
(`src/lcsas/db/schema.py:29-33` for fresh DBs;
`src/lcsas/db/schema.py:263-296` for the v4→v5 migration that added
`CONSOLIDATING`).

**Which commands trigger each transition (for this category):**

| From → To                      | Trigger                                            | Source                                                                                       |
| ------------------------------ | -------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `BURNED → VERIFIED`            | `lcsas verify --mark-verified` / `catalog import-receipts` w/ `verify_passed=true` | `src/lcsas/cli/main.py:1280-1290`                                                            |
| `VERIFIED → CONSOLIDATING`     | `lcsas consolidate --execute` (just before staging) | `src/lcsas/cli/main.py:1483-1485`, `src/lcsas/consolidate/merger.py:99-113`                  |
| `CONSOLIDATING → VERIFIED`     | Staging failure during `--execute` (auto-rollback) | `src/lcsas/cli/main.py:1494-1498`, `src/lcsas/consolidate/merger.py:115-122`                 |
| `CONSOLIDATING → DEPRECATED`   | `lcsas consolidate --deprecate` (post-verification) | `src/lcsas/cli/main.py:1431-1439`, `src/lcsas/consolidate/merger.py:84-97`                   |
| `VERIFIED → DEPRECATED`        | `lcsas consolidate --deprecate` (no `--execute` first) | Same as above (also legal per `VALID_TRANSITIONS`).                                       |
| `DEPRECATED → DESTROYED`       | No first-class CLI command yet — see *Gaps*       | `src/lcsas/db/volumes.py:31`                                                                 |

`update_status` itself enforces the table and, on the
`* → DEPRECATED` edge, additionally runs `check_deprecation_safe`
inside a SAVEPOINT to refuse the transition if any pack would become
unreplicated (`src/lcsas/db/volumes.py:118-159`,
`:246-270`).  Pass `force=True` to override (logged at WARNING —
`src/lcsas/db/volumes.py:160-164`).

**Per-location lifecycle (`volume_copies`):** independently from the
volume-wide status above, each physical copy has its own status of
`ACTIVE`/`DEPRECATED`/`DESTROYED`
(`src/lcsas/db/schema.py:93-109`).  Helpers
`deprecate_copy(conn, volume_id, location)` and
`destroy_copy(conn, volume_id, location)` toggle these
(`src/lcsas/db/volume_copies.py:158-184`).  *The deprecation triggered
by `consolidate --deprecate` does NOT cascade into `volume_copies`* —
operators with multi-copy archives must run `deprecate_copy` per
location separately, and no top-level CLI surfaces this yet.

**Gaps for this section:**
- No first-class `lcsas destroy <volume>` command exists; the
  `DEPRECATED → DESTROYED` edge is only reachable by directly calling
  `update_status` (e.g. from a script).  Recording physical shredding
  / disc destruction in the audit trail is therefore manual.
- No CLI tool propagates a volume-level DEPRECATED status to the
  matching `volume_copies` rows.
- No test asserts that a forced `DESTROYED → *` (terminal-state)
  transition is rejected by `update_status` (it should be — empty
  transition set at `src/lcsas/db/volumes.py:32`).

---

## Audit trail: `volume_events`

Every consolidate/deprecate transition leaves a row in
`volume_events` (`src/lcsas/db/schema.py:134-147`).  The table
enforces a CHECK on `event_type`:
`VERIFY_PASS`, `VERIFY_FAIL`, `VERIFY_FAIL_REBURN`, `ECC_REPAIR`,
`LOCATION_MOVE`, `CONDITION_CHECK`, `NOTE`
(`src/lcsas/db/schema.py:138-140`).  These constants are mirrored in
the Python layer (`src/lcsas/db/volume_events.py:22-31`).

**Which workflow writes which event:**

| Workflow                                      | Event                          | Detail field                                |
| --------------------------------------------- | ------------------------------ | ------------------------------------------- |
| `consolidate --execute`                       | `NOTE` (per source volume)     | `"Status changed: VERIFIED → CONSOLIDATING"` |
| Staging-failure auto-rollback                 | `NOTE` (per source volume)     | `"Status changed: CONSOLIDATING → VERIFIED"` |
| `consolidate --deprecate` (success)           | `NOTE` (per source volume)     | `"Status changed: <prev> → DEPRECATED"`     |
| `catalog validate` (PASS/FAIL)                | **none** — see *Gaps*          | n/a                                         |
| `catalog rebuild`                             | **none** — only writes rows merged from the disc catalogs | n/a |
| `loc move` (`cmd_location`, out of category)  | `LOCATION_MOVE` (record-keeping precedent) | `<free text>`                  |

Events are inserted by `add_event` (`src/lcsas/db/volume_events.py:34-75`)
and `update_status` (`src/lcsas/db/volumes.py:181-187`,
`:146-152`).  Queries: `get_events_for_volume`,
`get_latest_event`, `get_events_by_type`
(`src/lcsas/db/volume_events.py:88-149`).

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
- `src/lcsas/db/schema.py:134-147` — `volume_events` DDL
- `src/lcsas/db/volume_events.py:1-149` — CRUD + valid-type set
- `src/lcsas/db/volumes.py:146-187` — automatic `NOTE` insertion on
  status transitions
