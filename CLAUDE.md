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

**LCSAS** orchestrates three external tools — **Rustic** (deduplication/encryption), **Xorriso** (ISO mastering), and **DVDisaster** (RS03 ECC) — to produce petabyte-scale optical cold-storage archives.

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
- **Meta-volume** — a separate bootable disc (`meta/`) bundles static x86_64 binaries (rustic, xorriso, python3), LCSAS source, and a `restore.sh` script so full recovery is possible with nothing pre-installed.

### Database schema

Schema version 5. Key tables: `repositories`, `packs`, `volumes`, `volume_packs` (M:M), `snapshots`, `locations`, `volume_copies`, `sessions`, `volume_events` (audit trail). Volume lifecycle: `STAGING → BURNING → BURNED → VERIFIED → DEPRECATED → DESTROYED`.
