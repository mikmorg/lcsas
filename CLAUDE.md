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
```

Pytest writes temp files to `/var/tmp/pytest-lcsas` and cleans them up automatically. Integration tests are skipped unless the required binaries are present; unit tests run with no external dependencies.

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
2. **Bin-pack** — `binpack/algorithm.py` runs first-fit-decreasing to fill volumes to the configured media size (BD25, MDISC100, BDXL100, TEST_TINY — defined in `config/media.py`).
3. **Stage** — `staging/builder.py` hardlinks packs into a staging tree; `staging/metadata.py` (`HolographicInjector`) copies the complete SQLite catalog and per-repo Rustic metadata (index, snapshots, keys) onto every disc so any single disc is self-describing.
4. **ISO** — `iso/xorriso.py` calls xorriso to master the staging directory into an ISO.
5. **ECC** — `ecc/dvdisaster.py` augments the ISO with DVDisaster RS03 error correction.
6. **Burn** — `burn/orchestrator.py` drives the full pipeline and records volume copies and locations in the catalog.

Restore is the mirror: `restore/planner.py` generates a disc pick list; `restore/executor.py` fetches packs from mounted ISOs; then `rustic restore` runs against the assembled cache. `restore/restic_fallback.py` provides a pure-Python AES/zstd restore path requiring no binaries.

### Module map

| Package | Role |
|---------|------|
| `cli/` | argparse entry-point (`lcsas` command, 15+ subcommands) |
| `config/` | TOML config loader, media type definitions |
| `db/` | SQLite catalog — schema (v5), connection, frozen-dataclass models, CRUD, queries |
| `rustic/` | Protocol-based subprocess wrapper + JSON output parser |
| `packs/` | Mirror scanner, pack-to-snapshot delta analysis |
| `binpack/` | FFD bin-packing algorithm |
| `staging/` | Staging tree builder, holographic metadata injector, cleanup |
| `iso/` | Xorriso wrapper |
| `ecc/` | DVDisaster wrapper |
| `burn/` | Full burn pipeline orchestrator |
| `restore/` | Restore planner, executor, pure-Python fallback, standalone env builder |
| `consolidate/` | Volume merger (collapses redundant packs across discs) |
| `meta/` | Meta-volume builder (bootable disaster-recovery disc with bundled binaries + source) |
| `utils/` | Hashing, label generation, two-level hex pack layout, subprocess base, fs helpers |

### Key design patterns

- **Protocol-based wrappers** — external tools (`RusticRunner`, xorriso, dvdisaster) are injected via Protocol interfaces, enabling unit tests to use fakes without subprocess calls.
- **Holographic catalog** — the complete SQLite catalog is burned onto every disc so recovery never requires a central server.
- **Multi-tenancy** — multiple Rustic repos share physical volumes; each repo is encrypted with its own key; the catalog tracks per-repo ownership.
- **Zero runtime dependencies** — the entire codebase uses only the Python standard library (`zstandard` is optional). This is intentional so the restore path works on a bare system.
- **Meta-volume** — a separate bootable disc (`meta/`) bundles per-target static binaries (rustic, xorriso, python3), LCSAS source, and a `restore.sh` script so full recovery is possible with nothing pre-installed.  Phase 21 added per-target bundling for six rust-triples (Linux x86_64/aarch64/armv7 musl, macOS arm64/x86_64, Windows x86_64-gnu).

### Recovery cascade (intent + reality)

The recovery tiers are documented in `recovery/docs/TIERS.txt` and dispatched by `recovery/scripts/restore.sh`:

| Tier | Binary | Intent |
|---|---|---|
| **1 (primary)** | our C89 `lcsas-restore` built against vendored sqlite+zstd | The DURABLE path. C89 ABI-stable for 35 years. Depends only on a kernel + libc. No third-party RUNTIME dependency. |
| 2 (fallback) | upstream `rustic-static` | Hedge in case tier 1 won't run on a given host. Pinned upstream artifact (`recovery/UPSTREAM.sha256`). |
| 3 (last resort) | bundled CPython + `standalone_restorer.py` | Last-resort recovery if tiers 1+2 both fail. Pinned upstream CPython (`python-build-standalone`). |

**Vendoring vs runtime dependency:** sqlite + zstd live as C source in `recovery/vendored/` and we compile them ourselves alongside our own code — that's not a "third party runtime dependency", it's source we ship and audit (pinned in `recovery/MANIFEST.sha256`). Rustic and CPython ARE runtime dependencies (we ship opaque prebuilt artifacts pinned in `recovery/UPSTREAM.sha256`).

**Intent:** the bare path (tier 1) must work with nothing but kernel + libc + the `lcsas-restore` binary off the meta-volume. No `pip install`, no package manager, no upstream release matrix that still needs to exist decades from now. Cross-platform tier-1 coverage as of Phase 21.12: all 6 approved targets — Linux x86_64/aarch64/armv7 musl, Windows-gnu, macOS Intel + Apple Silicon (the macOS pair via `zig cc -target <arch>-macos`, no Apple SDK required). See `docs/CROSS_PLATFORM_META_RFC.md` §6 Q6.

**Disc-integrity layer (beneath the cascade):** the tiers choose *which tool* reads the bytes; two guards keep the bytes themselves intact. DVDisaster RS03 ECC (wrapped around every burned image) repairs bit-rotted sectors, and tier-1 then authenticates every blob (Poly1305 MAC + SHA-256 content hash) and *rejects* corrupt data — so disc corruption is repaired-or-rejected, never silently restored. The RS03 repair path is validated against the real dvdisaster binary by `tests/integration/test_ecc_repair.py` (opt-in `LCSAS_ECC_REPAIR=1`: below-threshold damage → byte-identical repair; above-threshold → fails loud). The hardware-only physical-disc drill is `recovery/docs/PHYSICAL_DISC_VALIDATION.txt`. See `recovery/docs/TIERS.txt` "DISC-INTEGRITY LAYER".

### Database schema

Schema version 5. Key tables: `repositories`, `packs`, `volumes`, `volume_packs` (M:M), `snapshots`, `locations`, `volume_copies`, `sessions`, `volume_events` (audit trail). Volume lifecycle: `STAGING → BURNING → BURNED → VERIFIED → DEPRECATED → DESTROYED`.
