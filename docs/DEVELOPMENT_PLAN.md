# LCSAS — Development Plan

> Generated: 2026-02-18 | Baseline: commit `f0dbc60` | 492 tests passing

---

## 1. Project Summary

LCSAS (Linux Cold Storage Archival Suite) orchestrates Rustic, Xorriso, and
DVDisaster to write deduplicated, encrypted data packs onto optical media
(BD-R, M-Disc) and LTO tape. It provides:

- **CDC-based infinite incrementalism** via Rustic (zero-cost renames/moves)
- **Multi-tenant encryption isolation** (per-repo keys, shared physical media)
- **Holographic indexing** (complete SQLite catalog on every disc)
- **Multi-copy location tracking** (burn N copies, each tagged to a location)
- **Session-based multi-volume staging** (decouple ISO creation from burning)
- **DVDisaster RS03 ECC** (image-level error correction)
- **Self-contained disaster recovery** (meta-volume with bundled tools + restore.sh)

The codebase consists of 28 Python source modules under `src/lcsas/`, zero
runtime pip dependencies (pure stdlib), and 492 tests (477 unit + 15
integration) passing at baseline.

---

## 2. Known Issues

### 2.1 Duplicate Function Definitions in queries.py

**File:** `src/lcsas/db/queries.py`
**Lines:** 267–413

Three functions are defined twice — the second definition silently shadows
the first:

| Function | First definition | Shadowing duplicate |
|---|---|---|
| `get_packs_at_location()` | Line 267 | Line 371 |
| `get_packs_missing_at_location()` | Line 283 | Line 387 |
| `get_location_summary()` | Line 334 | Line 413 |

**Risk:** If the two versions differ in behavior, the first (possibly
correct) version is unreachable. If identical, it's dead code that will
cause confusion during maintenance.

**Fix:** Compare both versions, keep the correct one, delete the duplicate.
Add tests covering all three functions if not already present.

### 2.2 Architecture Doc Stale Layout

**File:** `docs/architecture.md`, Section 4 "Staging Directory Layout"

Shows flat `data/aabbccdd...` layout, but production code (`ingest_volume`
in `executor.py`) now uses two-level `data/<prefix>/<sha>` layout matching
restic 0.14+ DefaultLayout.

**Fix:** Update the architecture doc to reflect the two-level layout.

---

## 3. Feature Gaps

### 3.1 CLI Commands Parsed But Not Dispatched

These commands have argparse parsers defined in `build_parser()` but no
handler in `dispatch()` — they fall through to "not yet implemented":

| Command | Library Code Exists | Library Tested |
|---|---|---|
| `restore plan` | `RestorePlanner.generate_pick_list()` | Yes (5 tests) |
| `restore exec` | `RestoreExecutor.prepare_cache/ingest/execute` | Yes (11 tests) |
| `verify` | `XorrisoRunner.verify_disc()`, `DVDisasterRunner.verify_iso()` | Yes (mocked) |
| `consolidate` | `VolumeMerger.plan_consolidation/deprecate_sources` | Yes (3 tests) |
| `burn-iso` | `XorrisoRunner.burn_iso()` | Yes (mocked) |
| `burn` (no --session) | `BurnOrchestrator.prepare/execute` | Yes (legacy path) |

### 3.2 Missing CLI Commands (No Parser)

| Command | Purpose | Library Code |
|---|---|---|
| `scan` | Discover new packs from mirror, register in catalog | `scan_mirror_packs()` + `DeltaAnalyzer` exist |
| `catalog rebuild` | Recover catalog from disc's `catalog.db` | None — needs new code |
| `config check` | Validate TOML config file | None |

### 3.3 Missing Subsystems

| Feature | Description | Impact |
|---|---|---|
| **Snapshot persistence** | `snapshots` table exists but is never populated. Rustic parser produces `SnapshotInfo` objects but nothing writes them to DB. | Catalog can't answer "which packs does snapshot X need?" without a live mirror. Restore planning requires the mirror to be online. |
| **Prune sync** | No workflow to mark packs as `is_pruned` in the catalog when Rustic prunes them. | `is_pruned` flags drift from reality over time; consolidation analysis becomes inaccurate. |
| **Verification tracking** | No way to record "disc X verified on date Y" or schedule periodic re-verification. | Users can't track which discs are overdue for integrity checks. |
| **Catalog rebuild from disc** | Holographic catalog is written to every disc but there's no tooling to extract and adopt it as the new master. | The core disaster recovery promise (any disc bootstraps recovery) is aspirational. |
| **Volume event audit trail** | Original design docs proposed a `volume_events` table for lifecycle tracking. Not implemented. | Can't answer "when was this disc last verified?" or trace operational history. |

### 3.4 Documentation Gaps

| Gap | Description |
|---|---|
| **Key backup strategy** | README shows key generation but never mentions backup (paper key, Cryptosteel, separate USB). Keys are the single point of total failure. |
| **Meta-volume platform limitation** | Bundler copies ELF x86_64 binaries + .so files. Won't work on ARM64 or incompatible glibc. Not documented. |
| **Architecture doc staleness** | Staging layout section shows flat pack layout; should show two-level. |

---

## 4. Iterative Development Plan

Work is organized into phases. Each phase is a self-contained commit with
full test coverage. Phases are ordered by dependency and priority.

### Phase 1: Bug Fixes & Code Cleanup

**Goal:** Fix known defects, clean up dead code, update stale docs.
**Estimated scope:** Small. No new features.

| Task | Details | Tests |
|---|---|---|
| 1.1 Fix duplicate definitions in `queries.py` | Compare both versions of `get_packs_at_location`, `get_packs_missing_at_location`, `get_location_summary`. Keep correct version, delete duplicate. | Verify existing tests pass. Add tests if any of the 3 functions lack coverage. |
| 1.2 Update architecture.md staging layout | Change flat `data/aabbccdd...` to two-level `data/aa/aabbccdd...` in Section 4. | N/A (documentation). |
| 1.3 Add key backup guidance to README | Add a "Key Management" section after key generation covering: paper key, Cryptosteel/Coldcard, separate USB, safe deposit. Emphasize that key loss = total data loss. | N/A (documentation). |
| 1.4 Document meta-volume platform limitations | Note in README that the meta-volume is x86_64 Linux only; recovery machine must have compatible glibc. | N/A (documentation). |

**Exit criteria:** All 492+ tests pass. No duplicate definitions. Docs accurate.

---

### Phase 2: Wire `scan` CLI Command

**Goal:** Give users a standalone command to discover and register new packs
from their mirrors without committing to staging.

| Task | Details | Tests |
|---|---|---|
| 2.1 Add `scan` subcommand parser | `lcsas scan [--repo NAME]` — scans configured mirrors for new packs, registers them in the catalog, prints delta summary. | Unit tests: parser accepts args, help text. |
| 2.2 Implement `cmd_scan()` handler | Call `scan_mirror_packs()` per repo, then `DeltaAnalyzer.register_new_packs()`, print summary of new/total/unarchived. | Unit tests: mock scanner, verify DB registration. Integration test: real directory with fake pack files. |
| 2.3 Wire to `dispatch()` | Add `elif args.command == "scan"` routing. | CLI dispatch test. |

**Exit criteria:** `lcsas scan` discovers packs, registers them, prints summary.
Covered by ≥5 new tests.

---

### Phase 3: Wire `restore plan` + `restore exec` CLI Commands

**Goal:** Make the restore workflow accessible from the CLI. This is the
single most critical missing feature — a backup tool must be able to restore.

| Task | Details | Tests |
|---|---|---|
| 3.1 Implement `cmd_restore_plan()` | Accept snapshot ID + optional `--repo`. Call `rustic restore --dry-run` to get required pack hashes. Call `RestorePlanner.generate_pick_list()`. Format and print pick list (volumes, pack counts, sizes, missing packs). | Unit tests with mocked RusticRunner: correct output formatting, handles missing packs, handles `--repo` filter. ≥4 tests. |
| 3.2 Implement `cmd_restore_exec()` | Accept snapshot ID + `--target` + `--password-file` + optional `--repo`. Call planner to get pick list. For each volume: prompt user to mount/specify path → call `RestoreExecutor.ingest_volume()`. After all volumes ingested → call `RestoreExecutor.execute_restore()`. | Unit tests with mocked executor: correct orchestration sequence, handles already-cached packs, error on missing volumes. ≥5 tests. |
| 3.3 Wire both to `dispatch()` | Add routing for `restore plan` and `restore exec`. | CLI dispatch tests: both subcommands route correctly. |
| 3.4 Integration test | End-to-end: create repo → backup files → stage → create ISOs → restore plan → restore exec → verify byte-for-byte match. | 1 integration test (may take 30-60s with real restic). |

**Exit criteria:** Full restore workflow works from CLI. ≥12 new tests.

---

### Phase 4: Wire `verify` CLI Command

**Goal:** Post-burn verification and periodic integrity auditing from the CLI.

| Task | Details | Tests |
|---|---|---|
| 4.1 Implement `cmd_verify()` | Accept `--volume LABEL` or `--iso PATH`. For disc: call `DVDisasterRunner.verify_iso()` or `XorrisoRunner.verify_disc()`. Print result. Update volume status to VERIFIED or DEPRECATED based on outcome. | Unit tests with mocked runners: pass/fail paths, status updates. ≥4 tests. |
| 4.2 Wire to `dispatch()` | Add routing. | CLI dispatch test. |

**Exit criteria:** `lcsas verify --volume X` works. ≥5 new tests.

---

### Phase 5: Wire `consolidate` CLI Command

**Goal:** Allow users to reclaim space by merging volumes with high prune ratios.

| Task | Details | Tests |
|---|---|---|
| 5.1 Implement `cmd_consolidate()` | Accept volume IDs + `--target-media TYPE`. Call `VolumeMerger.plan_consolidation()`. Print plan (source volumes, active packs, target count). Prompt for confirmation. Execute burn pipeline on active packs. Call `deprecate_sources()`. | Unit tests with mocked merger: plan display, confirmation prompt, abort path. ≥4 tests. |
| 5.2 Wire to `dispatch()` | Add routing. | CLI dispatch test. |

**Exit criteria:** `lcsas consolidate` works. ≥5 new tests.

---

### Phase 6: Wire `burn-iso` CLI Command

**Goal:** Enable the remote/deferred burn workflow (burn a single ISO on a
machine without the catalog DB).

| Task | Details | Tests |
|---|---|---|
| 6.1 Implement `cmd_burn_iso()` | Accept `ISO_PATH` + `--device` + `--verify`. Call `XorrisoRunner.burn_iso()`. Optionally verify. Generate a receipt JSON file alongside the ISO. | Unit tests with mocked xorriso: correct args, receipt generation. ≥3 tests. |
| 6.2 Wire to `dispatch()` | Add routing for `burn-iso`. | CLI dispatch test. |

**Exit criteria:** `lcsas burn-iso` works. ≥4 new tests.

---

### Phase 7: Snapshot Persistence

**Goal:** Populate the `snapshots` table so the catalog is self-sufficient
for restore planning without requiring a live mirror.

| Task | Details | Tests |
|---|---|---|
| 7.1 Create `db/snapshots.py` CRUD module | `register_snapshot()`, `get_snapshot()`, `list_snapshots()`, `get_snapshots_for_repo()`. Following the pattern of `db/packs.py`. | ≥6 CRUD tests. |
| 7.2 Create `db/snapshot_packs.py` | New junction table `snapshot_packs(snapshot_id, pack_id)` mapping snapshots to required packs. CRUD: `link_packs_to_snapshot()`, `get_required_packs()`. Schema migration to v3. | ≥4 tests for junction table + migration test. |
| 7.3 Persist snapshots during scan/stage | After `rustic snapshots --json` → parse → register snapshots in DB. After `rustic restore --dry-run` → record pack dependencies. | ≥3 tests. |
| 7.4 Enhance `restore plan` to use DB snapshots | If mirror is unavailable, fall back to `snapshot_packs` table for pack resolution. | ≥2 tests: mirror-available path, mirror-lost path. |

**Exit criteria:** Snapshots + pack deps stored in DB. Restore plan works
without live mirror. ≥15 new tests. Schema version bumped to 3.

---

### Phase 8: Catalog Rebuild from Disc

**Goal:** Implement `lcsas catalog rebuild` to recover the master catalog
from any LCSAS volume's embedded `catalog.db`.

| Task | Details | Tests |
|---|---|---|
| 8.1 Implement `cmd_catalog_rebuild()` | Accept `--source PATH` (mount point or extracted dir containing `catalog.db`). Copy the disc's `catalog.db` to the configured `db_path`. Run status fixup: `UPDATE volumes SET status='VERIFIED', closed_at=created_at WHERE status='STAGING'`. Print summary of recovered state. | Unit tests: file copy, status fixup SQL, error handling. ≥4 tests. |
| 8.2 Add `catalog rebuild` subcommand parser | Parser + dispatch wiring. | CLI test. |
| 8.3 Integration test | Create archive → extract catalog.db from ISO → delete master DB → rebuild → verify restore plan still works. | 1 integration test. |

**Exit criteria:** Full catalog recovery from a single disc. ≥6 new tests.

---

### Phase 9: Prune Synchronization

**Goal:** Keep the catalog's `is_pruned` flags accurate when Rustic
prunes old snapshots.

| Task | Details | Tests |
|---|---|---|
| 9.1 Implement prune sync logic | Scan mirror packs on disk → compare against catalog → any pack in catalog but missing from disk (and not already pruned) gets `is_pruned=1`. | ≥3 tests: no drift, some pruned, all pruned. |
| 9.2 Add `scan --prune-sync` flag | Extend the `scan` command to optionally detect and mark pruned packs. | ≥2 tests. |
| 9.3 Report pruning in `status` | Show pruned pack count and reclaimable bytes in `lcsas status`. | ≥1 test. |

**Exit criteria:** `is_pruned` stays accurate. ≥6 new tests.

---

### Phase 10: Verification Tracking

**Goal:** Record verification events with timestamps so users can track
which discs are overdue for integrity checks.

| Task | Details | Tests |
|---|---|---|
| 10.1 Add `volume_events` table | `volume_events(id, volume_id, event_type, timestamp, details)`. Event types: CREATED, BURNED, VERIFIED, MOVED, DEPRECATED, DESTROYED. Schema migration to v4. | ≥3 tests: create events, query by volume, query by type. |
| 10.2 Emit events from existing operations | `burn_session()` → BURNED event. `verify` → VERIFIED event. `location move` → MOVED event. | ≥3 tests: verify events are emitted. |
| 10.3 Add `lcsas verify --status` | Show last verification date per volume, highlight overdue (>N months). | ≥2 tests. |

**Exit criteria:** All lifecycle events tracked. ≥8 new tests. Schema v4.

---

### Phase 11: Stage Dry-Run & Config Validation

**Goal:** Operational convenience features.

| Task | Details | Tests |
|---|---|---|
| 11.1 `stage --dry-run` | Print volume plan (count, sizes, pack assignments) without creating ISOs or modifying DB. | ≥3 tests. |
| 11.2 `lcsas config check` | Validate TOML config: required fields present, paths exist, repos reference valid mirrors, media types valid. | ≥5 tests: valid config passes, missing fields, bad paths, unknown media type. |

**Exit criteria:** Both features work. ≥8 new tests.

---

## 5. Phase Dependencies

```
Phase 1 (bugs/docs) ─── no deps, do first
    │
Phase 2 (scan) ─── prerequisite for Phase 9
    │
Phase 3 (restore CLI) ─── highest user value
    │
Phase 4 (verify CLI) ─── prerequisite for Phase 10
    │
Phase 5 (consolidate CLI)
    │
Phase 6 (burn-iso CLI)
    │
Phase 7 (snapshot persistence) ─── enhances Phase 3
    │
Phase 8 (catalog rebuild) ─── requires Phase 3
    │
Phase 9 (prune sync) ─── requires Phase 2
    │
Phase 10 (verification tracking) ─── requires Phase 4
    │
Phase 11 (dry-run + config check)
```

Phases 2–6 are independent of each other and can be done in any order.
The recommended order (above) prioritizes user-facing value.

---

## 6. Test Coverage Targets

| Phase | New Tests (est.) | Cumulative Total |
|---|---|---|
| Baseline | — | 492 |
| Phase 1 | 3–5 | ~495 |
| Phase 2 | 5–7 | ~502 |
| Phase 3 | 12–15 | ~515 |
| Phase 4 | 5–6 | ~521 |
| Phase 5 | 5–6 | ~527 |
| Phase 6 | 4–5 | ~531 |
| Phase 7 | 15–18 | ~548 |
| Phase 8 | 6–8 | ~555 |
| Phase 9 | 6–7 | ~562 |
| Phase 10 | 8–10 | ~572 |
| Phase 11 | 8–10 | ~582 |

**Target:** ≥580 tests by plan completion, all passing.

---

## 7. Definition of Done (Per Phase)

- [ ] All new code has corresponding unit tests
- [ ] All existing tests still pass (zero regressions)
- [ ] `ruff check` passes (no lint errors)
- [ ] Changes committed with descriptive commit message
- [ ] README/docs updated if user-facing behavior changed

---

## 8. Out of Scope (Future Roadmap)

These items are acknowledged but deferred beyond this plan:

| Item | Rationale |
|---|---|
| LTO tape I/O wrapper | Requires tape hardware; schema already supports LTO media types |
| Cloud tier (S3/rclone) | Architectural extension; current design could accommodate |
| Multi-session optical writing | Adds complexity; current whole-disc model is simpler and sufficient |
| Cross-platform meta-volume | Would require static binaries or multi-arch builds |
| Dashboard / rich status TUI | Nice-to-have; basic `status` command suffices |
| Email/webhook notifications | Operational tooling; external orchestration (cron) can handle |
