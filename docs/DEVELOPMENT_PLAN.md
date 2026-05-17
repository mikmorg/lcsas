# LCSAS — Development Plan

> Generated: 2026-02-18 | Last refresh: 2026-05-16 | Current: 1131 tests passing (commit `c4bb777`)

This document captures the development arc of LCSAS from its initial audit
through Phase 20.  All planned phases are complete; the file is retained
as historical record and as a forward-looking pointer to the active
roadmap in §8.

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
- **DVDisaster RS03 ECC** (image-level error correction, always-on for production media)
- **Self-contained disaster recovery** (meta-volume with bundled tools + restore.sh)
- **Pure-Python restore fallback** (no external binaries needed for disaster recovery)
- **Prune synchronization** (tracks pruned packs for consolidation analysis)
- **Deprecation safety** (prevents deprecating volumes with unreplicated packs)
- **3-tier recovery cascade** (prebuilt static `lcsas-restore` → vendored `rustic-static`
  → pure-Python `standalone_restorer.py`; tiers 1–2 are Python-free)

The codebase has zero runtime pip dependencies (pure stdlib; `zstandard` is
optional for the pure-Python tier-3 fallback) and **1131 tests passing**
across unit + integration + e2e at commit `c4bb777`.

---

## 2. Known Issues — all RESOLVED

> All known issues from the original audit have been resolved. Section
> retained for historical reference.

- **2.1 Duplicate function definitions in `queries.py`** — Fixed in Phase 1.
- **2.2 Architecture doc stale layout** — Fixed in Phase 20; staging layout
  now correctly shows the two-level `data/<prefix>/<hash>` pattern.

---

## 3. Feature Gaps — all RESOLVED

> All feature gaps identified in the original audit have been addressed.

### 3.1 CLI Commands Parsed But Not Dispatched — RESOLVED

All commands (`restore plan`, `restore exec`, `verify`, `consolidate`,
`burn-iso`, `burn`) are wired to handlers and covered by tests.
Completed across Phases 3–6.

### 3.2 Missing CLI Commands — RESOLVED

| Command | Status |
|---|---|
| `scan` | ✅ Phase 2 (extended with `--no-prune-sync` in Phase 16) |
| `repo remove` | ✅ Phase 19 |
| `location` | ✅ Phase 12 |
| `catalog import` / `validate` / `rebuild` | ✅ Phase 12 + later test coverage (#62, #63) |
| `recovery build` / `test` / `manifest` / `verify` | ✅ Phase 11 (extended #64) |
| `session list` | ✅ Phase 13 (tested #65) |
| `restore standalone` (formerly `restore from-disc`) | ✅ Phase 11 |

### 3.3 Missing Subsystems — RESOLVED

| Feature | Status |
|---|---|
| Prune sync | ✅ Phase 16 — `detect_pruned()`, integrated into `cmd_scan()` |
| Verification tracking | ✅ Phase 14 — `volume_events` table, `VERIFY_PASS`/`VERIFY_FAIL` events |
| Volume event audit trail | ✅ Phase 12 (schema v4) — `volume_events` with lifecycle tracking |
| Holographic catalog injection | ✅ Phase 12 — `HolographicInjector.inject_catalog()` |
| C89/POSIX-sh recovery driver | ✅ — `recovery/scripts/restore.sh` implements the 3-tier cascade |

### 3.4 Documentation Gaps — RESOLVED

All three gaps from the original audit are now filled.  Cross-references
for verification:

| Gap | Location | Verified |
|---|---|---|
| Key backup strategy | `README.md` §"Key Backup (Critical)" (≈ line 88) covers paper key, Cryptosteel, USB | ✅ |
| Meta-volume platform limitation | `README.md` §"Platform Limitations" (≈ line 525) documents x86_64 ELF requirement and ARM64/RISC-V workarounds | ✅ |
| Architecture doc staging layout | `docs/architecture.md` §"Staging Directory Layout" (≈ line 230) shows the correct two-level layout | ✅ |

---

## 4. Iterative Development Plan — all phases COMPLETE

All 20 phases shipped.  Summary table:

| Phase | Title | Status |
|---|---|---|
| 1 | Bug fixes & code cleanup | ✅ |
| 2 | Wire `scan` CLI command | ✅ |
| 3 | Wire `restore plan` + `restore exec` | ✅ |
| 4 | Wire `verify` CLI | ✅ |
| 5 | Wire `consolidate` CLI | ✅ |
| 6 | Wire `burn-iso` CLI | ✅ |
| 7 | Snapshot persistence | ✅ |
| 8 | Prune synchronization | ✅ (delivered as Phase 16) |
| 9 | Verification tracking | ✅ (delivered as Phase 12 + 14) |
| 10 | Stage dry-run + config validation | ✅ |
| 11 | 50-year survivability hardening | ✅ |
| 12 | Schema v4 (locations, volume_copies, burn_sessions, volume_events) | ✅ |
| 13 | Orchestrator refactoring (session-based staging) | ✅ |
| 14 | Verification pipeline (`cmd_verify --all`, event emission) | ✅ |
| 15 | Resilient restore (`PackSource`, alternates, `collect_failures`) | ✅ |
| 16 | Prune sync | ✅ |
| 17 | Two-level staging layout (`data/<prefix>/<hash>`) | ✅ |
| 18 | Pure-Python restore improvements (hardlink dedup, xattr) | ✅ |
| 19 | CLI & operational (locked_connection, repo remove, XDG db_path) | ✅ |
| 20 | Documentation refresh (architecture, security, plan) | ✅ |

Detailed sub-tasks for each phase remain in
[`PHASE_12_20_PLAN.md`](PHASE_12_20_PLAN.md) for reference.

### Post-Phase-20 cleanup (May 2026)

After phase 20 the team executed a follow-up wave of cleanups via merged
PRs #16–#77 covering: location-event audit, `--config` flag honoring,
receipt-provenance persistence, unknown-`--location` rejection, ECC
verify-or-repair on mounted ISOs, LTO removal, test media simplification,
`lcsas db export` removal (redundant with holographic injection),
recovery cascade collapse from 5 tiers to 3, vestigial-flag removal
(`--key-file`, `--skip-ecc`), always-on ECC for production media,
Python-fallback chain pruned from `restore.bat`, `cmd_burn_legacy`
removal, test-coverage backfill for every CLI handler, e2e script
pytest wrapping, and a GitHub Actions test workflow with real rustic.

### Out-of-band session (May 2026)

A subsequent session rebased an abandoned local DRY refactor, fixed a
line-length lint, and repaired 9 latent CI failures across
`test_meta_builder`, `test_meta_volume_restore`, `test_pure_python_restore`,
`test_session_pipeline`, and the e2e harness.  See commits `67b9d8f`,
`0e23ed0`, `01d7658`, `2be7400`, `c4bb777`.

---

## 5. Phase Dependencies

All phases complete; dependency graph retained at
[`PHASE_12_20_PLAN.md`](PHASE_12_20_PLAN.md).

---

## 6. Test Coverage

**Current:** 1131 tests passing (1094 unit + 36 integration + 1 e2e),
7 environment-gated skips (PTY for interactive restore, root for cdemu,
Windows toolchain for cross-build), 0 failures.

Line coverage by `pytest --cov=lcsas`: 79% overall.  The largest
coverage gaps are:

| Module | Coverage | Notes |
|---|---|---|
| `meta/live/restore_wizard.py` | 23% | TUI; hard to unit-test without mocking the terminal |
| `recovery/build.py` | 24% | Drives the C-source build; covered by integration tests |
| `meta/bootable.py` | 46% | Alpine live-boot installer |
| `iso/xorriso.py` | 69% | Some xorriso edge cases unmocked |
| `cli/main.py` | 70% | Mostly interactive prompts in `cmd_restore_exec` / `cmd_consolidate` |

---

## 7. Definition of Done (Per Phase)

- [x] All new code has corresponding unit tests
- [x] All existing tests still pass (zero regressions)
- [x] `ruff check` passes
- [x] `mypy --strict` passes
- [x] Changes committed with descriptive commit message
- [x] README / workflow docs updated when user-facing behavior changes

---

## 8. Future Roadmap

These items are deferred beyond Phase 20 and are not currently being
worked on.  Listed roughly in descending order of "addresses a real
limitation users have hit."

| Item | Status | Notes |
|---|---|---|
| **Cross-platform meta-volume** | Phase 21.1–21.9 SHIPPED 2026-05-17; Phase 21.10.a SHIPPED (gap disclosure); 21.10.b–21.12 pending | Multi-arch bundling for six targets (`x86_64-unknown-linux-musl`, `aarch64-unknown-linux-musl`, `armv7-unknown-linux-gnueabihf`, `aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-pc-windows-gnu`).  Tier 2 (rustic-static) + tier 3 (CPython) are bundled for every target.  Tier 1 (our `lcsas-restore`) is currently host-arch-only — see [`CROSS_PLATFORM_META_RFC.md`](CROSS_PLATFORM_META_RFC.md) §6 Q6 for the gap and the Phase 21.10.b/21.11/21.12 fix sequence. |
| **Coverage backfill** | Phase 21 added 90+ tests across cmd_consolidate, the dispatcher, bundlers, and verify paths; restore_wizard.py and bootable.py remain the biggest gaps | The largest remaining single-module gaps are `meta/live/restore_wizard.py` (~23% covered, TUI) and `meta/bootable.py` (~46%, Alpine live-boot installer).  Both would benefit from a dedicated test-mocking strategy. |
| **Cloud tier (S3/rclone)** | Out of scope | Architectural extension; current design could accommodate.  Would extend the storage-tier model from HOT/WARM/COLD to HOT/WARM/COLD/REMOTE. |
| **Multi-session optical writing** | Out of scope | Adds complexity; current whole-disc model is simpler and sufficient. |
| **Dashboard / rich status TUI** | Out of scope | Nice-to-have; the current `lcsas status` and `verify --all` are sufficient for operator use. |
| **Email / webhook notifications** | Out of scope | Operational tooling; external orchestration (cron, systemd timers) can handle. |
