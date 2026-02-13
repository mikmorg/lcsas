# LCSAS — Linux Cold Storage Archival Suite

## Architecture Reference

> Synthesized from the four-document research progression:
> 1. Cold Backup Workflow for Terabytes
> 2. Encrypted Pack File Volume Management
> 3. Decentralized Content-Addressable Archival Architecture
> 4. LCSAS: Decentralized Immutable Archival Architecture

---

## 1. Mission Statement

LCSAS orchestrates the archival of **Rustic** (restic-compatible) repository data
onto cold storage media — primarily optical (Blu-ray, M-DISC) and tape (LTO) —
producing self-describing, error-corrected, verifiable volumes that can be
restored independently without access to any central server or catalog.

### Core Design Tenets

| Tenet | Description |
|---|---|
| **Content-addressable** | Every pack file is identified by its SHA-256 hash; deduplication and verification are intrinsic |
| **Holographic metadata** | Each volume carries everything needed to restore from it alone: index files, snapshot manifests, encryption keys, and a catalog snapshot |
| **Multi-tenant** | Multiple Rustic repositories (family, work, friends, etc.) share volumes; the catalog tracks per-repo ownership |
| **Immutable media** | Write-once optical and WORM tape guarantee bitrot resistance; ECC (RS03 via dvdisaster) adds a second defence layer |
| **Decentralized** | No server dependency; the SQLite catalog is replicated onto every volume and can be rebuilt from any set of volumes |

---

## 2. Storage Tier Model

```
Tier 0 — HOT         NAS / local disk (Rustic mirrors, active repos)
Tier 1 — WARM        Staging area on fast SSD/HDD (temporary, pre-burn)
Tier 2 — COLD        Optical media / LTO tape (permanent archive)
```

### Supported Media Types

| Media | Capacity | ECC Overhead | Usable | Type |
|-------|----------|-------------|--------|------|
| BD-R 25 GB | 25 GB | 20% | 20 GB | Optical |
| BD-R 50 GB | 50 GB | 20% | 40 GB | Optical |
| BDXL 100 GB | 100 GB | 20% | 80 GB | Optical |
| M-DISC 25 GB | 25 GB | 20% | 20 GB | Optical |
| M-DISC 100 GB | 100 GB | 20% | 80 GB | Optical |
| LTO-8 | 12 TB | 5% | 11.4 TB | Tape |
| LTO-9 | 18 TB | 5% | 17.1 TB | Tape |
| TEST_TINY | 1 MB | 0% | 1 MB | Test |
| TEST_SMALL | 10 MB | 10% | 9 MB | Test |

---

## 3. Data Model

### Rustic Repository Anatomy

A Rustic/restic repository contains:

```
repo/
├── config              # Repository ID + encryption params
├── keys/               # Encryption key files
├── index/              # Pack-to-blob mapping
├── snapshots/          # Snapshot manifests (JSON)
├── data/               # Pack files (content-addressed blobs)
│   ├── 00/             # Two-level hex prefix directories
│   │   ├── 00abc...    # Individual pack files
│   │   └── 00def...
│   ├── 01/
│   └── ...
└── locks/              # Advisory locks (not archived)
```

**Pack files** are the atomic unit of archival. Each pack:
- Contains one or more compressed, encrypted blobs (file chunks, tree nodes, etc.)
- Is identified by its SHA-256 content hash
- Is immutable once written
- Ranges from ~4 MB to ~100 MB in typical configurations

### SQLite Catalog Schema

```sql
-- Tracks schema migrations
schema_version (version INTEGER)

-- Physical media volumes
volumes (
    volume_id    INTEGER PRIMARY KEY,
    label        TEXT UNIQUE,        -- e.g. LCSAS_BD_2026_001
    uuid         TEXT UNIQUE,
    media_type   TEXT,               -- BD25, MDISC100, LTO8, etc.
    capacity     INTEGER,            -- Raw capacity in bytes
    used_bytes   INTEGER DEFAULT 0,
    location     TEXT,               -- Physical storage location
    status       TEXT,               -- STAGING → BURNED → VERIFIED → DEPRECATED
    created_at   TEXT,
    closed_at    TEXT
)

-- Logical repositories (multi-tenant)
repositories (
    repo_id      TEXT PRIMARY KEY,   -- Short identifier: "family", "work"
    display_name TEXT,
    mirror_path  TEXT                -- Path on NAS mirror
)

-- Individual pack files
packs (
    pack_id      INTEGER PRIMARY KEY,
    sha256       TEXT UNIQUE,
    size_bytes   INTEGER,
    repo_id      TEXT REFERENCES repositories,
    is_pruned    BOOLEAN DEFAULT 0,
    created_at   TEXT
)

-- Many-to-many: which packs are on which volumes
volume_packs (
    volume_id    INTEGER REFERENCES volumes,
    pack_id      INTEGER REFERENCES packs,
    PRIMARY KEY (volume_id, pack_id)
)

-- Snapshot records (informational)
snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    repo_id      TEXT REFERENCES repositories,
    hostname     TEXT,
    paths        TEXT,               -- JSON array
    tags         TEXT,               -- JSON array
    created_at   TEXT
)
```

### Volume Lifecycle States

```
STAGING → BURNED → VERIFIED → (active use)
                              → DEPRECATED (after consolidation)
```

---

## 4. Burn Pipeline

The burn pipeline is the central workflow — transforming unarchived packs into
verified, self-describing cold storage volumes.

### Pipeline Stages

```
1. SCAN        Scan Rustic mirror for pack files, record SHA-256 + size
2. DELTA       Compare scanned packs against catalog → identify unarchived
3. BINPACK     First-fit-decreasing to fill a volume (capacity - ECC - metadata reserve)
4. STAGE       Hardlink (or copy) selected packs into staging directory
5. METADATA    Inject holographic metadata (index, snapshots, keys, config, catalog)
6. ISO         Create ISO 9660 image (Rock Ridge + Joliet, ISO level 3) via xorriso
7. ECC         Augment ISO with RS03 error correction via dvdisaster
8. BURN        Write to physical media via cdrecord (DAO mode)
9. VERIFY      Read-back verification (SHA-256 comparison or dvdisaster verify)
10. CATALOG    Update SQLite: create volume record, link packs, mark closed
```

### Staging Directory Layout

```
LCSAS_BD_2026_003/
├── data/                          # Pack files (hardlinked from mirror)
│   ├── aabbccdd...                # Flat layout (SHA-256 hash as filename)
│   └── ...
├── metadata/
│   ├── family/                    # Per-repo metadata
│   │   ├── config
│   │   ├── index/
│   │   ├── keys/
│   │   └── snapshots/
│   └── work/
│       ├── config
│       ├── index/
│       ├── keys/
│       └── snapshots/
├── catalog.db                     # SQLite catalog snapshot
└── volume_info.json               # Volume identity + manifest
```

### volume_info.json

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "label": "LCSAS_BD_2026_003",
  "media_type": "BD25",
  "created_at": "2026-03-15T10:30:00Z",
  "pack_count": 142,
  "total_bytes": 19832156000,
  "repositories": ["family", "work"],
  "sha256_manifest": {
    "aabbccdd...": 4194304,
    "eeff0011...": 8388608
  }
}
```

---

## 5. Restore Pipeline

### Phase 1: Plan

1. User provides a snapshot ID (or list of pack hashes)
2. `rustic restore --dry-run` determines required packs
3. `RestorePlanner` maps pack hashes → volume labels via catalog
4. Generates a **pick list**: which volumes to load, in what order (minimize disc swaps)

### Phase 2: Execute

1. User inserts volumes per pick list
2. System reads packs from disc into a local **pack cache**
3. Once all required packs are cached, `rustic restore` runs against the reconstructed mirror
4. Cache is cleaned up after successful restore

### Disaster Recovery (No Catalog)

If the central catalog is lost:
1. Insert any LCSAS volume
2. Read `catalog.db` from the volume → bootstrap a new catalog
3. Insert remaining volumes → merge their catalogs
4. Full catalog is reconstructed without any external dependency

---

## 6. Volume Consolidation

Over time, volumes accumulate pruned (dead) packs as Rustic's forget/prune
cycle removes old snapshots. Consolidation reclaims space:

1. **Analyse** — identify volumes where >N% of packs are pruned
2. **Plan** — collect all *active* packs from source volumes, estimate target volume count
3. **Re-burn** — run the standard burn pipeline with the active pack set
4. **Deprecate** — mark source volumes as DEPRECATED (do not physically destroy yet)
5. **Verify** — confirm all active packs now exist on new volumes with ≥2 copies

---

## 7. Multi-Tenant Encryption Model

Each repository maintains its own encryption independently:

- Rustic encrypts all data at the repository level before pack files are written
- LCSAS handles only opaque, pre-encrypted pack files
- Per-repo `keys/` directory is included in holographic metadata
- Password files are referenced in config but never stored in the catalog
- A single volume may contain packs from multiple repos — each repo's keys decode only its own data

---

## 8. Redundancy & Integrity

### Error Correction

- **RS03** (dvdisaster) augments the ISO with Reed-Solomon parity data
- Typical overhead: 20% for optical, 5% for tape
- Enables recovery from surface scratches, partial media degradation
- Can be verified offline: `dvdisaster --verify`

### Redundancy Strategy

- Critical packs should exist on ≥2 physical volumes
- The `redundancy_report` query identifies packs with fewer than N copies
- Consolidation naturally creates additional copies of surviving packs

### Verification

- Post-burn: read-back the entire disc and verify SHA-256 of every pack
- Periodic: `dvdisaster --verify` on stored volumes
- Catalog cross-check: compare mirror packs against catalog records

---

## 9. Module Architecture

```
lcsas/
├── config/          # MediaType enum, TOML settings, repo definitions
├── db/              # SQLite: schema, connection, models, CRUD, queries
├── utils/           # Hashing, filesystem ops, label generation
├── binpack/         # First-fit-decreasing volume packing
├── packs/           # Mirror scanner, delta analysis
├── rustic/          # Subprocess wrapper (Protocol), JSON parsers
├── iso/             # Xorriso wrapper (Protocol) — ISO creation & burning
├── ecc/             # DVDisaster wrapper (Protocol) — RS03 ECC
├── staging/         # Staging directory builder, holographic metadata
├── burn/            # Burn orchestrator (pipeline conductor)
├── restore/         # Pick-list planner, restore executor
├── consolidate/     # Volume merger, deprecation
└── cli/             # argparse CLI with subcommands
```

### Key Design Patterns

| Pattern | Usage |
|---------|-------|
| **Protocol classes** | `RusticRunner`, `XorrisoRunner`, `DVDisasterRunner` — swap real subprocess implementations for mocks |
| **Frozen dataclasses** | All DB models are immutable value objects |
| **Pure parsers** | JSON parsing functions are separated from subprocess invocation for independent testing |
| **Hardlink staging** | Packs are hardlinked (not copied) from mirror for zero-cost staging on same filesystem |
| **WAL mode** | SQLite uses WAL journal for safe concurrent reads |

---

## 10. CLI Commands

```
lcsas init          [--db-path PATH]              Initialize catalog database
lcsas repo add      NAME MIRROR_PATH [--pw-file]  Register a repository
lcsas repo list                                    List registered repositories
lcsas status        [--repo REPO]                  Show archive status summary
lcsas burn          [--media TYPE] [--dry-run]     Run the burn pipeline
                    [--iso-only PATH] [--skip-ecc]
                    [--device DEV]
lcsas restore plan  SNAPSHOT_ID [--repo REPO]      Generate restore pick list
lcsas restore exec  SNAPSHOT_ID --target DIR       Execute restore from volumes
lcsas consolidate   VOL_IDS... --target-media TYPE Plan/execute consolidation
lcsas verify        [--volume LABEL]               Verify volume integrity
lcsas db export     [--output FILE]                Export catalog as JSON
```

---

## 11. Testing Strategy

### Test Media Types

| Type | Capacity | ECC | Purpose |
|------|----------|-----|---------|
| `TEST_TINY` | 1 MB | 0% | Unit tests — fast, fits in RAM |
| `TEST_SMALL` | 10 MB | 10% | Integration tests — exercises ECC path |

### Test Categories

- **Unit tests** (`tests/unit/`): All modules tested in isolation with in-memory SQLite and mocked subprocesses
- **Integration tests** (`tests/integration/`): End-to-end pipeline with real filesystem operations, requires `rustic`/`xorriso`/`dvdisaster` on PATH
- **Fixtures**: `conftest.py` provides `memory_db`, `populated_db` (20 packs across 5 volumes), `test_config`, `tmp_mirror` (fake Rustic repo layout)

### Running Tests

```bash
make test-unit          # Fast, no external tools needed
make test-integration   # Requires rustic, xorriso, dvdisaster
make test-all           # Both
make coverage           # With coverage report
```

---

## 12. Label Convention

```
{PREFIX}_{MEDIA}_{YEAR}_{SEQ}
```

Examples:
- `LCSAS_BD_2026_001` — First Blu-ray volume of 2026
- `LCSAS_MD_2026_003` — Third M-DISC volume
- `LCSAS_LTO_2026_001` — First LTO tape

The sequence number is global across all media types for a given prefix,
monotonically increasing, ensuring no label collisions.
