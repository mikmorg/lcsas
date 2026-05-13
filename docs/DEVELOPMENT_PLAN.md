# LCSAS — Development Plan

> Generated: 2026-02-18 | Updated: 2026-03-29 | Current: 853 tests passing (commit `a9adebc`)

---

## 1. Project Summary

LCSAS (Linux Cold Storage Archival Suite) orchestrates Rustic, Xorriso, and
DVDisaster to write deduplicated, encrypted data packs onto optical media
(BD-R, M-Disc). It provides:

- **CDC-based infinite incrementalism** via Rustic (zero-cost renames/moves)
- **Multi-tenant encryption isolation** (per-repo keys, shared physical media)
- **Holographic indexing** (complete SQLite catalog on every disc)
- **Multi-copy location tracking** (burn N copies, each tagged to a location)
- **Session-based multi-volume staging** (decouple ISO creation from burning)
- **DVDisaster RS03 ECC** (image-level error correction)
- **Self-contained disaster recovery** (meta-volume with bundled tools + restore.sh)
- **Pure-Python restore fallback** (no external binaries needed for disaster recovery)
- **Prune synchronization** (tracks pruned packs for consolidation analysis)
- **Deprecation safety** (prevents deprecating volumes with unreplicated packs)

The codebase consists of 30+ Python source modules under `src/lcsas/`, zero
runtime pip dependencies (pure stdlib), and 853 tests (838 unit + 15
integration) passing at the latest commit.

---

## 2. Known Issues

> All known issues from the original audit have been resolved. This section
> is retained for historical reference.

### ~~2.1 Duplicate Function Definitions in queries.py~~ — RESOLVED

Fixed in Phase 1 (commit `f0dbc60`). Duplicate definitions removed, tests added.

### ~~2.2 Architecture Doc Stale Layout~~ — RESOLVED

Fixed in Phase 20. Architecture doc updated to reflect two-level `data/<prefix>/<sha>`
layout, schema v4 tables, 15% ECC overhead, full volume lifecycle, and new subsystems.

---

## 3. Feature Gaps

> All feature gaps identified in the original audit have been addressed.
> This section is retained for historical reference.

### ~~3.1 CLI Commands Parsed But Not Dispatched~~ — RESOLVED

All commands (`restore plan`, `restore exec`, `verify`, `consolidate`,
`burn-iso`, `burn`) are now wired to handlers and tested. Completed across
Phases 3–6.

### ~~3.2 Missing CLI Commands (No Parser)~~ — RESOLVED

| Command | Status |
|---|---|
| `scan` | ✅ Implemented in Phase 2, extended with `--no-prune-sync` in Phase 16 |
| `repo remove` | ✅ Implemented in Phase 19 |
| `location` | ✅ Implemented in Phase 12 |
| `catalog import` | ✅ Implemented in Phase 12 |

### ~~3.3 Missing Subsystems~~ — RESOLVED

| Feature | Status |
|---|---|
| **Prune sync** | ✅ Phase 16 — `detect_pruned()`, integrated into `cmd_scan()` |
| **Verification tracking** | ✅ Phase 14 — `volume_events` table, `VERIFY_PASS`/`VERIFY_FAIL` events |
| **Volume event audit trail** | ✅ Phase 12 (schema v4) — `volume_events` table with lifecycle tracking |

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
| 3.4 Integration test | End-to-end: create repo → backup files → stage → create ISOs → restore plan → restore exec → verify byte-for-byte match. | 1 integration test (may take 30-60s with real rustic). |

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

### Phase 7: Snapshot Persistence ✅ (partially complete)

**Goal:** Populate the `snapshots` table so the catalog records which
snapshots exist per repo.

| Task | Details | Status |
|---|---|---|
| 7.1 Create `db/snapshots.py` CRUD module | `upsert_snapshot()`, `bulk_upsert_snapshots()`, `get_snapshot()`, `list_snapshots()`, `get_snapshots_for_repo()`. | ✅ Done |
| 7.2 Persist snapshots during scan | `cmd_scan()` calls `rustic snapshots --json` → parse → `bulk_upsert_snapshots()`. | ✅ Done |

**Note on `snapshot_packs` junction table:** This was originally proposed
(mapping each snapshot to its required pack hashes) to enable mirror-offline
restore planning. After analysis, this is **not needed**:

1. Rustic's own `index/` files (already on every disc via holographic
   metadata) contain the pack-to-blob mapping. Running `rustic restore
   --dry-run` against a reconstructed cache from disc metadata answers
   "which packs does this snapshot need?" without any junction table.
2. Populating the junction would require running `rustic restore --dry-run`
   for *every* snapshot on *every* scan — O(snapshots) subprocess calls
   just to cache information rustic already stores.
3. In a true disaster (mirror lost), the user already has everything
   needed on disc: metadata, packs, and catalog.db.

**Exit criteria:** Snapshots persisted during scan. ✅ Complete.

---

### ~~Phase 8: Catalog Rebuild from Disc~~ — REMOVED

> **Rationale:** Each disc already carries a cumulative `catalog.db`
> (injected by `HolographicInjector.inject_catalog()`). The most recent
> disc's `catalog.db` is already the complete master catalog. Recovery
> is simply: `cp /mnt/disc/catalog.db ~/.config/lcsas/catalog.db`.
> No special tooling, schema migrations, or status fixups are needed.
> See architecture.md §5 "Disaster Recovery (No Catalog)" for details.

---

### Phase 8: Prune Synchronization ✅ (complete)

**Goal:** Keep the catalog's `is_pruned` flags accurate when Rustic
prunes old snapshots.

| Task | Details | Status |
|---|---|---|
| 8.1 Implement prune sync logic | `detect_pruned()` + `bulk_mark_pruned()` in db/packs.py | ✅ Done (Phase 16) |
| 8.2 Add `scan --no-prune-sync` flag | Prune sync runs by default with `lcsas scan`; `--no-prune-sync` disables. | ✅ Done (Phase 16) |
| 8.3 Report pruning in `status` | Included in consolidation analysis and status output. | ✅ Done |

**Exit criteria:** `is_pruned` stays accurate. ✅ Complete (6 tests added, Phase 16).

---

### Phase 9: Verification Tracking ✅ (complete)

**Goal:** Record verification events with timestamps so users can track
which discs are overdue for integrity checks.

| Task | Details | Status |
|---|---|---|
| 9.1 Add `volume_events` table | Schema v4 adds `volume_events` with event types: VERIFY_PASS, VERIFY_FAIL, ECC_REPAIR, LOCATION_MOVE, CONDITION_CHECK, NOTE. | ✅ Done (Phase 12) |
| 9.2 Emit events from existing operations | `cmd_verify()` emits VERIFY_PASS/VERIFY_FAIL events with details. | ✅ Done (Phase 14) |
| 9.3 Add `lcsas verify --status` | Verification status tracked via volume_events queries. | ✅ Done (Phase 14) |

**Exit criteria:** All lifecycle events tracked. ✅ Complete (schema v4, Phase 12 + Phase 14).

---

### Phase 10: Stage Dry-Run & Config Validation ✅ (complete)

**Goal:** Operational convenience features.

| Task | Details | Status |
|---|---|---|
| 10.1 `stage --dry-run` | Print volume plan (count, sizes, pack assignments) without creating ISOs or modifying DB. | ✅ Done |
| 10.2 `lcsas config check` | Validate TOML config: required fields present, paths exist, repos reference valid mirrors, media types valid. | ✅ Done |

**Exit criteria:** Both features work. ✅ Complete.

---

### Phase 11: 50-Year Survivability Hardening ✅ (complete)

**Goal:** Ensure the archive remains restorable by a non-technical user
over a 50-year term, even if the original archivist is deceased.
Full audit: see `docs/SURVIVABILITY.md`.

| Task | Details | Status |
|---|---|---|
| 11.1 Eliminate xorriso from restore.sh | Kernel `mount -o loop` primary, `7z x` fallback, bundled xorriso last resort. | ✅ Done |
| 11.2 Static musl rustic support | `static_rustic_path` in MetaVolumeBuilder. | ✅ Done |
| 11.3 Record tool versions on disc | `--version` for bundled tools → `volume_info.json`. | ✅ Done |
| 11.4 Bundle restic format spec | `docs/RESTIC_FORMAT_SPEC.md` on every meta-volume. | ✅ Done |
| 11.5 Human documentation on disc | `START_HERE.txt`, config fields for owner/description/key hints. | ✅ Done |
| 11.6 Key-to-repo mapping | `KEY_INFO.txt` on each disc. | ✅ Done |
| 11.7 Pure-Python restore fallback | `restore/restic_fallback.py` — AES-CTR decrypt, no C extensions. Hardlink dedup, xattr restoration, unsupported node handling. | ✅ Done (Phase 18) |

**Exit criteria:** Layered fallback (dynamic → static → python), format spec
on disc, all human docs present. ✅ Complete.

---

### Phase 12–20: Schema v4, Orchestrator, & Operational Hardening ✅ (complete)

Phases 12–20 were planned in `docs/PHASE_12_20_PLAN.md` and executed
sequentially with full test coverage. Summary:

| Phase | Title | Key Deliverables |
|---|---|---|
| 12 | Schema v4 | `locations`, `volume_copies`, `burn_sessions`, `session_volumes`, `volume_events` tables. Multi-location tracking, session-based burns, volume event audit trail. |
| 13 | Orchestrator Refactoring | `BurnOrchestrator.stage()` returns `StagingResult` → `SessionInfo` with per-volume ISO paths. Session DB records. |
| 14 | Verification Pipeline | `cmd_verify()` with `--all`/`--volume` flags. Emits `VERIFY_PASS`/`VERIFY_FAIL` events. Burn session verification. |
| 15 | Resilient Restore | `PackSource` dataclass, `PickListV2` with alternates, `collect_failures` mode, cross-location restore. |
| 16 | Prune Sync | `detect_pruned()` + `bulk_mark_pruned()`. Integrated into `cmd_scan()` with `--no-prune-sync` opt-out. |
| 17 | Staging Layout | Two-level `data/<prefix>/<hash>` layout in staging directory builder. |
| 18 | Pure-Python Restore | Hardlink deduplication, xattr restoration, unsupported node warnings in `restic_fallback.py`. |
| 19 | CLI & Operational | `locked_connection` for writes, `repo remove`, `consolidate --execute`, deprecation safety, TOML validation, XDG db_path. |
| 20 | Documentation | Architecture doc refresh, security considerations, development plan update. |

---

## 5. Phase Dependencies

```
Phase 1 (bugs/docs)            ✅ done
Phase 2 (scan)                 ✅ done
Phase 3 (restore CLI)          ✅ done
Phase 4 (verify CLI)           ✅ done
Phase 5 (consolidate + hardening) ✅ done
Phase 6 (burn-iso CLI)         ✅ done
Phase 7 (snapshot persistence) ✅ done
Phase 8 (prune sync)           ✅ done (Phase 16)
Phase 9 (verification tracking) ✅ done (Phases 12 + 14)
Phase 10 (dry-run + config)    ✅ done
Phase 11 (survivability)       ✅ done (Phases 11 + 18)
Phase 12 (schema v4)           ✅ done — locations, volume_copies, burn_sessions, events
Phase 13 (orchestrator refactor) ✅ done — session-based staging
Phase 14 (verification pipeline) ✅ done — verify CLI + events
Phase 15 (resilient restore)   ✅ done — PackSource, alternates, failure collection
Phase 16 (prune sync)          ✅ done — detect_pruned, bulk_mark_pruned
Phase 17 (staging layout)      ✅ done — two-level data/<prefix>/<hash>
Phase 18 (pure-Python restore) ✅ done — hardlink dedup, xattr, unsupported nodes
Phase 19 (CLI & operational)   ✅ done — locked_connection, repo remove, deprecation safety
Phase 20 (documentation)       ✅ done — architecture refresh, security considerations
```

All phases complete.

---

## 6. Test Coverage

| Phase | Status | Tests |
|---|---|---|
| Baseline | ✅ | 492 |
| Phase 1 (bugs/docs) | ✅ | ~495 |
| Phase 2 (scan) | ✅ | ~502 |
| Phase 3 (restore CLI) | ✅ | ~515 |
| Phase 4 (verify CLI) | ✅ | ~521 |
| Phase 5 (consolidate + hardening) | ✅ | ~540 |
| Phase 6 (burn-iso CLI) | ✅ | ~545 |
| Phase 7 (snapshot persistence) | ✅ | ~550 |
| Phase 8 (prune sync) | ✅ | ~556 |
| Phase 9 (verification tracking) | ✅ | ~564 |
| Phase 10 (dry-run + config) | ✅ | ~561 |
| Phase 11 (survivability) | ✅ | ~581 |
| Phase 12 (schema v4) | ✅ | 655 |
| Phase 13 (orchestrator refactor) | ✅ | 655 |
| Phase 14 (verification pipeline) | ✅ | 655 |
| Phase 15 (resilient restore) | ✅ | 664 |
| Phase 16 (prune sync) | ✅ | 670 |
| Phase 17 (staging layout) | ✅ | 670 |
| Phase 18 (pure-Python restore) | ✅ | 675 |
| Phase 19 (CLI & operational) | ✅ | 690 |
| Phase 20 (documentation) | ✅ | 690 |

**Current:** 853 tests passing (838 unit + 15 integration), 13 skipped.

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
| Cloud tier (S3/rclone) | Architectural extension; current design could accommodate |
| Multi-session optical writing | Adds complexity; current whole-disc model is simpler and sufficient |
| Cross-platform meta-volume | Would require static binaries or multi-arch builds |
| Dashboard / rich status TUI | Nice-to-have; basic `status` command suffices |
| Email/webhook notifications | Operational tooling; external orchestration (cron) can handle |
