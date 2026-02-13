# LCSAS — Linux Cold Storage Archival Suite

Petabyte-scale, offline-first archival system for Linux. Orchestrates **Rustic** (content-defined chunking), **Xorriso** (ISO mastering), and **DVDisaster** (error correction) to write deduplicated, encrypted data packs onto optical media (BD-R, M-Disc) and LTO tape.

## Key Capabilities

- **Infinite Incrementalism** — CDC ensures file moves/renames consume zero additional storage payload
- **Multi-Tenant Isolation** — distinct datasets encrypted with different keys coexist on the same disc
- **Holographic Indexing** — every disc carries a complete SQLite catalog of the entire archive
- **Local Mirror Strategy** — permanent hot tier enables instant consolidation without retrieving offsite media
- **Bit-Rot Immunity** — DVDisaster RS03 error correction wraps every ISO image

## Quick Start

```bash
# Install in development mode
make dev

# Run unit tests (no external tools required)
make test-unit

# Run all tests (requires rustic, xorriso, dvdisaster)
make test-all
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full synthesized architecture reference.

## Project Structure

```
src/lcsas/
├── cli/          # argparse CLI interface
├── config/       # Settings, media types, TOML config loading
├── db/           # SQLite schema, connection, CRUD, queries
├── rustic/       # Rustic subprocess wrapper (Protocol-based)
├── packs/        # Pack scanner and delta analysis
├── binpack/      # First-fit-decreasing bin packing algorithm
├── staging/      # Staging directory builder, holographic metadata
├── iso/          # Xorriso ISO creation wrapper
├── ecc/          # DVDisaster ECC wrapper
├── burn/         # Burn orchestrator (full pipeline)
├── restore/      # Restore planner and executor
├── consolidate/  # Volume merger / consolidation
└── utils/        # Hashing, filesystem helpers, label generation
```

## Testing

Uses `TEST_TINY` (1 MB) and `TEST_SMALL` (10 MB) media types for fast pipeline tests without optical hardware. Integration tests auto-skip when external tools are not installed.

```bash
make test-unit         # Pure Python, no external deps
make test-integration  # Requires rustic, xorriso, dvdisaster
make coverage          # HTML coverage report
```
