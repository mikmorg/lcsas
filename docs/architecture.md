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
onto cold optical storage media (Blu-ray, M-DISC) — producing self-describing,
error-corrected, verifiable volumes that can be restored independently without
access to any central server or catalog.

### Core Design Tenets

| Tenet | Description |
|---|---|
| **Content-addressable** | Every pack file is identified by its SHA-256 hash; deduplication and verification are intrinsic |
| **Holographic metadata** | Each volume carries everything needed to restore from it alone: index files, snapshot manifests, encryption keys, and a catalog snapshot |
| **Multi-tenant** | Multiple Rustic repositories (family, work, friends, etc.) share volumes; the catalog tracks per-repo ownership |
| **Immutable media** | Write-once optical media guarantees bitrot resistance; ECC (RS03 via dvdisaster) adds a second defence layer |
| **Decentralized** | No server dependency; the SQLite catalog is replicated onto every volume and can be rebuilt from any set of volumes |

---

## 2. Storage Tier Model

```
Tier 0 — HOT         NAS / local disk (Rustic mirrors, active repos)
Tier 1 — WARM        Staging area on fast SSD/HDD (temporary, pre-burn)
Tier 2 — COLD        Optical media (permanent archive)
```

### Supported Media Types

| Media | Capacity | ECC Overhead | Usable | Type |
|-------|----------|-------------|--------|------|
| BD-R 25 GB | 25 GB | 15% | ~21 GB | Optical |
| BD-R 50 GB | 50 GB | 15% | ~42 GB | Optical |
| BDXL 100 GB | 100 GB | 15% | ~85 GB | Optical |
| M-DISC 25 GB | 25 GB | 15% | ~21 GB | Optical |
| M-DISC 100 GB | 100 GB | 15% | ~85 GB | Optical |
| TEST_TINY | 1 MB | 0% | 1 MB | Test |

---

## 3. Data Model

### Rustic Repository Anatomy

A Rustic repository contains:

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

The on-disc restic/rustic pack format LCSAS reads and restores is specified in
[docs/RESTIC_FORMAT_SPEC.md](RESTIC_FORMAT_SPEC.md).

### SQLite Catalog Schema

The catalog is at **schema version 9** (`CURRENT_SCHEMA_VERSION` in
`src/lcsas/db/schema.py`) and defines **12 tables**: `schema_version`,
`volumes`, `repositories`, `packs`, `volume_packs`, `snapshots`,
`locations`, `volume_copies`, `burn_sessions`, `session_volumes`,
`volume_events`, and `key_escrow`.

```sql
-- Tracks schema migrations
schema_version (version INTEGER)

-- Physical media volumes
volumes (
    volume_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT UNIQUE NOT NULL,
    uuid         TEXT UNIQUE NOT NULL,
    media_type   TEXT NOT NULL,      -- BD25, MDISC100, BDXL100, etc.
    capacity_bytes INTEGER NOT NULL, -- Raw capacity in bytes
    used_bytes   INTEGER DEFAULT 0,
    location     TEXT DEFAULT 'Home_Shelf',
    status       TEXT,               -- STAGING → BURNING → BURNED → VERIFIED → DEPRECATED → DESTROYED
    created_at   DATETIME,
    closed_at    DATETIME,
    verified_at  DATETIME
)

-- Logical repositories (multi-tenant)
repositories (
    repo_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    mirror_path      TEXT NOT NULL,
    encryption_key_id TEXT DEFAULT '',
    created_at       DATETIME
)

-- Individual pack files
packs (
    pack_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256       TEXT UNIQUE NOT NULL,
    size_bytes   INTEGER NOT NULL,
    repo_id      TEXT REFERENCES repositories,
    is_pruned    INTEGER DEFAULT 0,
    created_at   DATETIME
)

-- Many-to-many: which packs are on which volumes
volume_packs (
    volume_id    INTEGER REFERENCES volumes,
    pack_id      INTEGER REFERENCES packs,
    PRIMARY KEY (volume_id, pack_id)
)

-- Snapshot records
snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    repo_id      TEXT REFERENCES repositories,
    hostname     TEXT,
    timestamp    DATETIME,
    paths        TEXT,               -- JSON array
    tags         TEXT,               -- JSON array
    description  TEXT
)

-- Named physical storage locations
locations (
    name         TEXT PRIMARY KEY,
    created_at   DATETIME,
    description  TEXT
)

-- Per-location copies of a volume (multi-copy tracking)
volume_copies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id    INTEGER REFERENCES volumes,
    location     TEXT REFERENCES locations,
    status       TEXT,               -- ACTIVE | DEPRECATED | DESTROYED
    burn_date    TEXT,
    notes        TEXT,
    iso_sha256   TEXT,
    last_verified_at DATETIME,
    media_serial TEXT,
    UNIQUE(volume_id, location)
)

-- Burn session batching
burn_sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   DATETIME,
    media_type   TEXT NOT NULL,
    status       TEXT,               -- STAGED | PARTIAL | COMPLETE | CLEANED
    staging_dir  TEXT NOT NULL
)

-- Volumes within a burn session
session_volumes (
    session_id   TEXT REFERENCES burn_sessions,
    volume_id    INTEGER REFERENCES volumes,
    iso_path     TEXT NOT NULL,
    iso_sha256   TEXT,
    PRIMARY KEY (session_id, volume_id)
)

-- Lifecycle event audit trail
volume_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id    INTEGER REFERENCES volumes,
    event_type   TEXT,               -- VERIFY_PASS, VERIFY_FAIL, ECC_REPAIR, LOCATION_MOVE, etc.
    event_date   DATETIME,
    location     TEXT REFERENCES locations,
    detail       TEXT
)

-- Recorded Shamir/SLIP-0039 key split per repo (KEY-08)
key_escrow (
    repo_id      TEXT PRIMARY KEY REFERENCES repositories,
    threshold    INTEGER NOT NULL, -- K in a K-of-N split
    shares       INTEGER NOT NULL, -- N total shares issued
    slip39_id    INTEGER NOT NULL, -- SLIP-0039 identifier
    split_at     DATETIME NOT NULL
)
```

> The `key_escrow` table records *that* a repository key was split into a
> K-of-N SLIP-0039 set (and when) — it never stores the shares or the key
> itself. Share words live only on the printed recovery cards. See the
> key-share format spec ([docs/KEY_SHARE_FORMAT.md](KEY_SHARE_FORMAT.md))
> and the `keyshare/` package.

### Volume Lifecycle States

```
STAGING → BURNING → BURNED → VERIFIED → (active use)
                                        → DEPRECATED → DESTROYED
```

Valid transitions:
| From | To |
|------|-----|
| STAGING | BURNING, DEPRECATED, DESTROYED |
| BURNING | BURNED, VERIFIED, STAGING (re-stage), DESTROYED |
| BURNED | VERIFIED, STAGING (re-burn), DESTROYED |
| VERIFIED | DEPRECATED, DESTROYED |
| DEPRECATED | DESTROYED |

Transitioning to DEPRECATED is blocked if the volume contains packs that
exist on no other active volume (unless `force=True`), preventing accidental
data loss.

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
│   ├── aa/                        # Two-level layout (first 2 hex chars)
│   │   ├── aabbccdd...            # Full SHA-256 hash as filename
│   │   └── ...
│   ├── bb/
│   │   └── ...
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

Each disc's ``catalog.db`` is a **cumulative snapshot** — the catalog
injected onto volume N contains all records from volumes 1 through N
(injected *after* the DB commit for volume N itself).  This means:

* **The highest-numbered disc's catalog is always the most complete.**
* No catalog merging is required — simply adopt the newest ``catalog.db``
  as the authoritative master.

Recovery procedure:

1. Insert any LCSAS volume (ideally the highest-numbered one available)
2. Copy ``catalog.db`` from the volume to a local path
3. This catalog already knows every volume and pack created up to and
   including that volume — no additional merge step is needed
4. If you only have older volumes, the catalog will be missing records
   for volumes produced *after* that disc was burned, but all prior
   data is fully described

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

- **RS03** Reed-Solomon parity is appended to every ISO, so a single image
  carries both data and its own ECC
- Write path: `ecc/dvdisaster.py` wraps the dvdisaster binary to augment ISOs
- Bare-metal read/repair path: the in-house C89 `lcsas-ecc` tool (FMT-01,
  `recovery/src/lcsas-ecc/`) reads and repairs the same RS03 format with **no
  dvdisaster binary required** — keeping the durable recovery path
  dependency-free
- Typical overhead: 15% for optical media
- Enables recovery from surface scratches, partial media degradation
- Format details: [docs/DVDISASTER_RS03_FORMAT.md](DVDISASTER_RS03_FORMAT.md)

### Redundancy Strategy

- Critical packs should exist on ≥2 physical volumes
- The `redundancy_report` query identifies packs with fewer than N copies
- Consolidation naturally creates additional copies of surviving packs

### Verification

- Staging-time: every selected pack is fully read and its SHA-256
  verified against the catalog hash before the ISO is mastered; a
  corrupt mirror pack aborts the stage before anything is written to
  the catalog (`CorruptPacksError`)
- Post-burn: media readability check (`xorriso -check_media`) plus a
  full exact-length device read-back hash of the burned ISO (BURN-04,
  implemented in `burn/orchestrator.py`); re-run on demand with
  `lcsas verify <label> --disc`
- On demand: `lcsas catalog validate <mounted-disc> --content` re-reads
  every pack file on a mounted disc and verifies its SHA-256 against
  the filename hash
- Periodic: `dvdisaster --verify` on stored volumes
- Catalog cross-check: compare mirror packs against catalog records

---

## 8.5 Security Considerations

### Catalog Encryption Tradeoff

The SQLite catalog database is **intentionally unencrypted** and embedded on
every disc as `catalog.db`. This is a deliberate design decision enabling
**self-describing recovery** — any single disc can bootstrap the entire archive
without needing the encryption key, external tools, or prior knowledge.

**What is exposed in the catalog:**

- File paths and directory names (from snapshot metadata)
- Hostnames that produced each backup
- Snapshot timestamps
- Repository identifiers and mirror paths
- Volume labels, locations, and pack SHA-256 hashes

**What remains encrypted:**

- All pack file contents (encrypted by Rustic/restic before LCSAS handles them)
- File contents, directory trees, and blob data within packs
- Encryption keys (stored on-disc in per-repo `keys/` directories, themselves
  requiring the repository password to unlock)

**Implication:** An attacker with physical access to a disc can learn *what*
was backed up (file paths, timestamps, hostnames) but cannot read *any* file
contents without the repository password. The full stolen-disc threat model
is in [docs/DISC_CONFIDENTIALITY.md](DISC_CONFIDENTIALITY.md).

**For highly sensitive archives**, consider:

- Using opaque repository names (e.g., `repo_a` instead of `medical_records`)
- A future metadata-scrubbed catalog variant that strips paths and hostnames
  (not yet implemented)
- Physical security of stored media

---

## 8.6 Multi-Location Tracking

Volumes can be burned in multiple copies, each stored at a different physical
location. The `locations` table names storage sites, and `volume_copies`
tracks per-location copies with independent status, verification timestamps,
and ISO checksums.

```
lcsas location add "Safe_Deposit_Box" --description "Bank vault, Box 42"
```

Burning the same session once per location (`lcsas burn --session latest
--location Home_Shelf`, then again with `--location Safe_Deposit_Box`)
records each physical copy independently. The `redundancy_report` query
shows packs across all active copies at all locations.

---

## 8.7 Session-Based Burns

Multi-volume staging is managed through **burn sessions** rather than
individual volume operations:

1. `cmd_burn()` creates a `burn_session` record with a UUID
2. The `BurnOrchestrator` stages all packs, bin-packs them into volumes,
   creates ISOs, and optionally applies ECC
3. Each generated volume is linked via `session_volumes` with its ISO path
   and SHA-256
4. Sessions progress through states: `STAGED → PARTIAL → COMPLETE → CLEANED`

This decouples ISO preparation from physical burning — ISOs can be created
on one machine and burned on another via `lcsas burn-iso`.

---

## 8.8 Resilient Restore

The restore pipeline handles degraded scenarios gracefully:

- **Multiple pack sources**: The pick list includes alternate volumes for each
  pack (`PackSource` dataclass with volume + location + priority)
- **Failure collection**: `ingest_volume()` can operate in `collect_failures`
  mode, recording which packs failed from a volume without aborting
- **Cross-location restore**: Packs can be sourced from any location where
  a copy exists; the planner minimizes disc swaps across locations
- **Pure-Python fallback**: If Rustic/restic is unavailable, a built-in
  Python implementation (`restic_fallback.py`) can decrypt and restore files
  using only stdlib (AES-CTR, scrypt, zstd decompression)

---

## 8.9 Prune Synchronization

When Rustic's `forget`/`prune` cycle removes old snapshots, pack files
disappear from the mirror. LCSAS detects this during `scan`:

1. `detect_pruned()` compares catalog packs against mirror contents
2. Any pack present in the catalog but absent from the mirror (and not
   already marked pruned) gets `is_pruned = 1`
3. This runs automatically with `lcsas scan` (disable with `--no-prune-sync`)
4. Accurate prune flags feed into consolidation analysis — identifying
   volumes where a high percentage of packs are dead



The Python source lives under `src/lcsas/` and is organised into **16
packages plus 5 top-level modules**:

```
lcsas/
├── config/          # MediaType enum, TOML settings, repo definitions
├── db/              # SQLite: schema (v9), connection, models, CRUD, queries
├── utils/           # Hashing, filesystem ops, two-level hex layout, label generation
├── binpack/         # First-fit-decreasing volume packing
├── packs/           # Mirror scanner, pack-to-snapshot delta analysis
├── rustic/          # Subprocess wrapper (Protocol), JSON parsers
├── iso/             # Xorriso wrapper (Protocol) — ISO mastering
├── ecc/             # DVDisaster RS03 wrapper (Protocol) — ECC augmentation
├── staging/         # Staging directory builder, holographic metadata injector
├── burn/            # Burn orchestrator (pipeline conductor)
├── restore/         # Pick-list planner, restore executor, pure-Python fallback
├── consolidate/     # Volume merger, deprecation
├── meta/            # Meta-volume builder (bundled per-target tools, source, restore.sh)
├── recovery/        # Recovery-bundle build driver for the tier-1/2/3 cascade
├── keyshare/        # SLIP-0039 share split/recombine, codec, printed recovery cards
├── cli/             # argparse CLI entry point (22 top-level + 33 nested subcommands)
├── constants.py     # Shared constants
├── exceptions.py    # Typed exception hierarchy
└── log.py           # Logging configuration
```

> Two of these — `recovery/` and `keyshare/` — back the durability story
> beyond the burn pipeline. `recovery/` drives the build of the
> meta-volume's three-tier restore cascade (see §9.10); `keyshare/`
> implements the SLIP-0039 K-of-N key split whose ledger is the
> `key_escrow` table (see §3). The in-tree C89 recovery tools live
> separately under the repo-root `recovery/src/` tree (`lcsas-restore`,
> `lcsas-ecc`, `lcsas-keyshare`, `lcsas-iso9660`, `lcsas-init`).

### Key Design Patterns

| Pattern | Usage |
|---------|-------|
| **Protocol classes** | `RusticRunner`, `XorrisoRunner`, `DVDisasterRunner` — swap real subprocess implementations for mocks |
| **Frozen dataclasses** | All DB models are immutable value objects |
| **Pure parsers** | JSON parsing functions are separated from subprocess invocation for independent testing |
| **Hardlink staging** | Packs are hardlinked (not copied) from mirror for zero-cost staging on same filesystem |
| **WAL mode** | SQLite uses WAL journal for safe concurrent reads |
| **Locked connections** | Write operations use `locked_connection()` context manager for exclusive DB access |
| **XDG paths** | Default database path follows XDG Base Directory specification (`~/.local/share/lcsas/archive.db`) |

---

## 9.10 Design Decision: Rustic vs Restic

### Background

Rustic and restic both implement the same content-addressable backup format
(repository format v1/v2). They produce **byte-identical on-disk structures** —
the same `config`, `keys/`, `index/`, `snapshots/`, and `data/` layout with
identical pack file checksums. A repository created by one tool can be read
and restored by the other without conversion.

### Decision

**Rustic is the primary tool** for all LCSAS write-path operations (backup,
prune, forget). On the *restore* side the durable path is **not** rustic:
the meta-volume cascade leads with our in-house C89 `lcsas-restore`
(tier 1), falls back to the pinned upstream `rustic-static` (tier 2), and
finally to bundled CPython + `standalone_restorer.py` (tier 3). See
"Recovery Cascade" below.

### Rationale

| Factor | Rustic | Restic |
|--------|--------|--------|
| **Language** | Rust | Go |
| **Performance** | Faster restore, lower memory | Mature, well-tested |
| **Hot/cold repos** | `--repo-hot` flag (native) | Not supported |
| **Static binary** | Single binary, no runtime deps | Single binary, no runtime deps |
| **JSON output** | `--json` flag, structured output | `--json` flag, compatible structure |
| **Repository format** | v1/v2 (identical to restic) | v1/v2 (identical to rustic) |
| **LCSAS integration** | Primary wrapper (`RusticRunner`) | Cascade-only, no dedicated wrapper |

### Recovery Cascade Order

The `restore.sh` script (`recovery/scripts/restore.sh`) on each meta-volume
dispatches three durability tiers, documented in `recovery/docs/TIERS.txt`:

| Tier | Binary | Intent |
|---|---|---|
| **1 (primary)** | in-house C89 `lcsas-restore`, built against vendored sqlite+zstd | The DURABLE path — ABI-stable, depends only on kernel + libc, **no third-party runtime dependency**. Cross-platform across all 6 approved targets. |
| 2 (fallback) | upstream `rustic-static` | Hedge if tier 1 will not run on a given host. Pinned artifact (`recovery/UPSTREAM.sha256`). |
| 3 (last resort) | bundled CPython + `standalone_restorer.py` | Final fallback if tiers 1+2 both fail. Pinned upstream CPython. |

`standalone_restorer.py` is a self-contained stdlib-only script (AES-CTR,
scrypt, zstd) that also rides on **every data disc**, so any single disc is
restorable with nothing but a Python 3.10+ interpreter.

**Vendoring vs runtime dependency:** sqlite + zstd ship as audited C source
in `recovery/vendored/` (pinned in `recovery/MANIFEST.sha256`) and we
compile them ourselves — that is source we own, not an opaque runtime
dependency. Rustic and CPython (tiers 2/3) *are* runtime dependencies: we
ship pinned prebuilt artifacts (`recovery/UPSTREAM.sha256`).

**Disc-integrity layer (beneath the cascade):** the tiers choose *which
tool* reads the bytes; two guards keep the bytes intact. DVDisaster RS03 ECC
(wrapped around every burned image) repairs bit-rotted sectors, and tier-1
then authenticates every blob (Poly1305 MAC + SHA-256 content hash) and
*rejects* corrupt data — disc corruption is repaired-or-rejected, never
silently restored.

The companion in-house C89 tools complete the bare-metal toolchain:
`lcsas-ecc` (FMT-01 — our own RS03 read/repair, no dvdisaster binary
required), `lcsas-keyshare` (SLIP-0039 share recombination), plus
`lcsas-iso9660` and `lcsas-init`. All live under `recovery/src/`.

> **Platform note:** tier-1 `lcsas-restore` is cross-built for all six
> approved targets (Linux x86_64/aarch64/armv7 musl, Windows x86_64-gnu,
> macOS Intel + Apple Silicon), so the durable path is available on every
> supported host rather than x86_64/glibc only. See the cross-platform meta
> RFC ([docs/CROSS_PLATFORM_META_RFC.md](CROSS_PLATFORM_META_RFC.md)).

### JSON Compatibility

Both tools produce structurally compatible JSON output. The `rustic/parser.py`
module handles minor field-name differences (e.g., `files_new` vs
`files_changed`) and normalizes into shared `BackupResult` / `SnapshotInfo`
dataclasses. A `restore_dry_run` compatibility note: restic uses `--dry-run`
while rustic uses `--dry-run` as well, but field names in the output may vary.
The parser gracefully handles both.

### Data Disc Standalone Restorer

Every data disc also carries `standalone_restorer.py` — a self-contained
single-file Python script (generated by `standalone_builder.py`) that combines
`_aes_pure.py` and `restic_fallback.py` with zero external dependencies.
This ensures any single data disc can be restored with only a Python 3.10+
interpreter, even without rustic, restic, or the meta-volume.

---

## 10. CLI Commands

The `lcsas` entry point (`src/lcsas/cli/main.py`) exposes **22 top-level
subcommands and 33 nested subcommands**. The selection below is
representative, not exhaustive — run `lcsas --help` (or read
`cli/main.py`) for the authoritative, current command surface.

```
lcsas init          [--db-path PATH]              Initialize catalog database
lcsas repo add      NAME MIRROR_PATH               Register a repository
lcsas repo list                                    List registered repositories
lcsas repo remove   REPO_ID [--force]              Remove a repository and its packs
lcsas scan          [--repo REPO] [--no-prune-sync] Discover new packs, mark pruned
lcsas status        [--redundancy] [--stale-copies] Show archive status summary
lcsas burn          --session ID [--media TYPE]    Burn staged ISOs to disc
                    [--location LOC] [--device DEV]
                    [--dry-run]
lcsas stage         [--media TYPE]                  Stage ISOs for deferred burning
lcsas burn-iso      ISO_PATH [--device DEV]        Burn a pre-built ISO to disc
                    [--emit-receipt PATH]           (cross-machine workflow)
                    [--label LABEL --location LOC]
lcsas restore plan  SNAPSHOT_ID --repo REPO        Generate restore pick list
lcsas restore exec  SNAPSHOT_ID TARGET_DIR         Execute restore from volumes
                    --repo REPO --password-file F
lcsas consolidate   VOL_IDS... --target-media TYPE Plan/execute consolidation
                    [--execute] [--deprecate]       (--deprecate retires sources
                                                    after a successful --execute)
lcsas location add  NAME [--description DESC]      Register a storage location
lcsas verify        [LABEL] [--disc] [--all]       Verify volume integrity
lcsas catalog import-receipts RECEIPT_FILES...     Import burn receipts
```

---

## 11. Testing Strategy

### Test Media Types

| Type | Capacity | ECC | Purpose |
|------|----------|-----|---------|
| `TEST_TINY` | 1 MB | 0% | Unit + integration tests — fast, fits in RAM |

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
- `LCSAS_BD25_2026_0001` — First BD25 volume of 2026
- `LCSAS_MDISC100_2026_0003` — Third MDISC100 volume

The sequence number is global across all media types for a given prefix,
monotonically increasing, ensuring no label collisions.
