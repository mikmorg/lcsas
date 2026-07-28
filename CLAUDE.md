# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (development)
make dev                          # pip install -e ".[dev]"

# Testing
make test-unit                    # Run unit tests (no external tools required)
make test-integration             # Run integration tests (requires rustic, xorriso, dvdisaster)
make test-all                     # Run all tests
make coverage                     # HTML + terminal coverage report

# Run a single test
pytest tests/unit/test_foo.py::test_bar -v

# Lint & type checking
make lint                         # ruff check src/ tests/
make lint-fix                     # ruff check --fix src/ tests/
make typecheck                    # mypy src/ (strict mode)

# The pre-push gate (also the default make target)
make gate                         # lint + typecheck + test-all + shell-coverage
```

Other gates to know about: `make audit-gate` (required before tier-1 C changes under `recovery/src/`), `make shell-coverage` (restore.sh line coverage), `make test-e2e` / `make test-recovery-hardening`, and `make meta-gate` (fetch-recovery + build-recovery). See the Makefile for the full list.

Pytest cleans up its temp files automatically (`tmp_path_retention_policy = "none"`). Integration tests are skipped unless the required binaries are present; unit tests run with no external dependencies.

## Architecture

**LCSAS** orchestrates three external tools — **Rustic** (deduplication/encryption), **Xorriso** (ISO mastering), and **DVDisaster** (RS03 ECC) — to produce durable, offline-first optical cold-storage archives (scaling to hundreds of discs across multiple repositories).

### Storage tier model

```
Tier 0 — HOT    NAS / local disk  (Rustic mirror repos, actively written)
Tier 1 — WARM   Staging SSD/HDD   (assembled ISOs, temporary)
Tier 2 — COLD   Optical           (burned discs; permanent)
```

### Data flow (burn pipeline)

1. **Scan** — `packs/scanner.py` walks the Rustic mirror and registers new pack files in the SQLite catalog (`db/`).
2. **Bin-pack** — `binpack/algorithm.py` runs first-fit-decreasing to fill volumes to the configured media size (CD700, BD25, BD50, BDXL100, MDISC25, MDISC50, MDISC100, TEST_TINY — defined in `config/media.py`). Every production capacity is a rung on dvdisaster's RS03 medium ladder (`ecc/dvdisaster.py`), and same-capacity tiers (M-Disc vs BD-R) must keep distinct enum values or `Enum` silently aliases them.
3. **Stage** — `staging/builder.py` hardlinks packs into a staging tree; `staging/metadata.py` (`HolographicInjector`) copies the complete SQLite catalog and per-repo Rustic metadata (index, snapshots, keys) onto every disc so any single disc is self-describing.
4. **ISO** — `iso/xorriso.py` calls xorriso to master the staging directory into an ISO.
5. **ECC** — `ecc/dvdisaster.py` augments the ISO with DVDisaster RS03 error correction.
6. **Burn** — `burn/orchestrator.py` drives the full pipeline and records volume copies and locations in the catalog.

Restore is the mirror: `restore/planner.py` generates a disc pick list; `restore/executor.py` fetches packs from mounted ISOs; then `rustic restore` runs against the assembled cache. `restore/restic_fallback.py` provides a pure-Python AES/zstd restore path requiring no binaries.

### Module map

| Package | Role |
|---------|------|
| `cli/` | argparse entry-point (`lcsas` command, 22 top-level subcommands: `burn`, `burn-iso`, `catalog`, `config`, `consolidate`, `copy`, `estate`, `init`, `key`, `location`, `meta`, `pack`, `recovery`, `repo`, `restore`, `scan`, `session`, `stage`, `staging`, `status`, `verify`, `volume`) |
| `config/` | TOML config loader, media type definitions |
| `db/` | SQLite catalog — schema (v9), connection, frozen-dataclass models, CRUD, queries |
| `rustic/` | Protocol-based subprocess wrapper + JSON output parser |
| `packs/` | Mirror scanner, pack-to-snapshot delta analysis |
| `binpack/` | FFD bin-packing algorithm |
| `staging/` | Staging tree builder, holographic metadata injector, cleanup |
| `iso/` | Xorriso wrapper |
| `ecc/` | DVDisaster wrapper |
| `burn/` | Full burn pipeline orchestrator |
| `restore/` | Restore planner, executor, pure-Python fallback, standalone env builder |
| `consolidate/` | Volume merger (collapses redundant packs across discs) |
| `meta/` | Meta-volume builder (disaster-recovery disc with bundled binaries + source; NOT bootable — the live-boot path was dropped per BOOT-01/BOOT-07) |
| `recovery/` | C89 recovery-binary cross-compilation harness (drives `lcsas recovery build`; builds tier-1 `lcsas-restore` against vendored sqlite+zstd) |
| `keyshare/` | Pure-Python SLIP-0039 Shamir key splitting (K-of-N escrow of repo keys) |
| `utils/` | Hashing, label generation, two-level hex pack layout, subprocess base, fs helpers |

### Key design patterns

- **Protocol-based wrappers** — external tools (`RusticRunner`, xorriso, dvdisaster) are injected via Protocol interfaces, enabling unit tests to use fakes without subprocess calls.
- **Holographic catalog** — the complete SQLite catalog is burned onto every disc so recovery never requires a central server.
- **Multi-tenancy** — multiple Rustic repos share physical volumes; each repo is encrypted with its own key; the catalog tracks per-repo ownership.
- **Zero runtime dependencies** — the entire codebase uses only the Python standard library (`zstandard` is optional). This is intentional so the restore path works on a bare system.
- **Meta-volume** — a separate disc (`meta/`) bundles per-target static binaries (rustic, xorriso, python3), LCSAS source, and a `restore.sh` script so full recovery is possible with nothing pre-installed. No LCSAS disc is bootable — the meta disc is mounted from a running OS (the live-boot path was quarantined to `experimental/boot/`). Phase 21 added per-target bundling for six rust-triples (Linux x86_64/aarch64/armv7 musl, macOS arm64/x86_64, Windows x86_64-gnu).

### Recovery cascade (intent + reality)

The recovery tiers are documented in `recovery/docs/TIERS.txt` and dispatched by `recovery/scripts/restore.sh`:

| Tier | Binary | Intent |
|---|---|---|
| **1 (primary)** | our C89 `lcsas-restore` built against vendored sqlite+zstd | The DURABLE path. C89 ABI-stable for 35 years. Depends only on a kernel + libc. No third-party RUNTIME dependency. |
| 2 (fallback) | upstream `rustic-static` | Hedge in case tier 1 won't run on a given host. Pinned upstream artifact (`recovery/UPSTREAM.sha256`). |
| 3 (last resort) | bundled CPython + `standalone_restorer.py` | Last-resort recovery if tiers 1+2 both fail. Pinned upstream CPython (`python-build-standalone`). |

**Vendoring vs runtime dependency:** sqlite + zstd live as C source in `recovery/vendored/` and we compile them ourselves alongside our own code — that's not a "third party runtime dependency", it's source we ship and audit (pinned in `recovery/MANIFEST.sha256`). Rustic and CPython ARE runtime dependencies (we ship opaque prebuilt artifacts pinned in `recovery/UPSTREAM.sha256`).

**Intent:** the bare path (tier 1) must work with nothing but kernel + libc + the `lcsas-restore` binary off the meta-volume. No `pip install`, no package manager, no upstream release matrix that still needs to exist decades from now. Cross-platform tier-1 coverage as of Phase 21.12: all 6 approved targets — Linux x86_64/aarch64/armv7 musl, Windows-gnu, macOS Intel + Apple Silicon (the macOS pair via `zig cc -target <arch>-macos`, no Apple SDK required). See `docs/CROSS_PLATFORM_META_RFC.md` §6 Q6.

**Disc-integrity layer (beneath the cascade):** the tiers choose *which tool* reads the bytes; two guards keep the bytes themselves intact. DVDisaster RS03 ECC (wrapped around every burned image) repairs bit-rotted sectors, and tier-1 then authenticates every blob (Poly1305 MAC + SHA-256 content hash) and *rejects* corrupt data — so disc corruption is repaired-or-rejected, never silently restored. The RS03 repair path is validated against the real dvdisaster binary by `tests/integration/test_ecc_repair.py` (below-threshold damage → byte-identical repair; above-threshold → fails loud). That proof runs weekly in CI (`.github/workflows/ecc-weekly.yml`: scheduled Mondays + on any PR touching `src/lcsas/ecc/`) and is also available locally via the opt-in `LCSAS_ECC_REPAIR=1` invocation. The hardware-only physical-disc drill is `recovery/docs/PHYSICAL_DISC_VALIDATION.txt`. See `recovery/docs/TIERS.txt` "DISC-INTEGRITY LAYER".

### Database schema

Schema version 9 (12 tables). Key tables: `repositories`, `packs`, `volumes`, `volume_packs` (M:M), `snapshots`, `locations`, `volume_copies`, `burn_sessions` + `session_volumes` (session/burn audit), `volume_events` (audit trail), `key_escrow` (recorded Shamir split: K/N + SLIP-0039 id, KEY-08). Volume lifecycle: `STAGING → BURNING → BURNED → VERIFIED → DEPRECATED → DESTROYED`, plus `CONSOLIDATING` (entered from VERIFIED while `consolidate/` merges a volume's packs onto a successor).

### Test tiers

`tests/` has four tiers, run in order by `make test-all` / `make gate`:

1. `tests/unit/` — no external tools required.
2. `tests/integration/` — real `rustic`/`xorriso`/`dvdisaster`/`cdemu` binaries, gated behind pytest markers (`requires_rustic`, etc.) so missing tools self-skip rather than fail.
3. `tests/e2e/` — full-pipeline scripted drills (e.g. `test_burn_verify_disc.py`, live-USB restore).
4. `tests/recovery_hardening/` — the last gate before a build ships (see `tests/recovery_hardening/README.md`). Each test exists because a specific bug reached production only after slipping through tiers 1–3; most are cheap static-analysis/stub-binary checks, plus a handful of opt-in slow gates behind env vars (`LCSAS_COVERAGE=1`, `LCSAS_SANITIZE=1`, `LCSAS_FAULT_INJECT=1`, `LCSAS_ZSTD_QEMU=1`).

**Blind-restore agent harness** (`tests/e2e/cdemu_blind_restore/`, driven by `make blind-restore*`): spawns a real Claude sub-agent with no prior context, hands it only the on-disc recovery docs + a simulated optical drive (`cdemu`), and scores whether it can complete a full disaster recovery from scratch. This is the acceptance test for the recovery cascade and docs, not just the code — it costs real LLM API spend (~$5/run) so it is opt-in (`LCSAS_BLIND_ACK_COST=1`) and not part of the default gate. Variants (`blind-restore-variants`) force specific cascade fallback paths (tier-1 missing, no-catalog, multi-tenant, split-key); the XFAIL ledger for known-failing variants lives at `tests/e2e/cdemu_blind_restore/XFAIL.list`.
