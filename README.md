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

## Usage Guide

This walkthrough covers a realistic home archival scenario: two family members and two personal directories, backed up onto M-Disc with two physical copies each (one at home, one offsite), with monthly incrementals.

### Scenario

| Directory | Repository | Encryption Key |
|-----------|-----------|----------------|
| `/srv/family1` | `family` | `/root/keys/family.key` |
| `/srv/family2` | `family` | `/root/keys/family.key` |
| `/srv/personal1` | `personal` | `/root/keys/personal.key` |
| `/srv/personal2` | `personal` | `/root/keys/personal.key` |

Both repos write to 100 GB M-Discs. Each burn cycle produces two copies — one for the home shelf, one for an offsite safe.

### 1. Install and Initialize

```bash
# Install LCSAS
pip install -e .

# Initialize the archive catalog
lcsas init --db-path /var/lib/lcsas/archive.db
```

### 2. Create Encryption Keys

Each repository gets its own key so that compromise of one key does not expose the other dataset.

```bash
# Generate separate keys
mkdir -p /root/keys
dd if=/dev/urandom bs=64 count=1 2>/dev/null | base64 > /root/keys/family.key
dd if=/dev/urandom bs=64 count=1 2>/dev/null | base64 > /root/keys/personal.key
chmod 600 /root/keys/*.key
```

### 3. Initialize Rustic Repositories (Local Mirrors)

LCSAS uses Rustic to create deduplicated, encrypted local mirrors. Each mirror is a permanent hot tier that persists on your server.

```bash
# Family mirror — one repo backing up two source directories
rustic init \
  --repo /mnt/mirror/family \
  --password-file /root/keys/family.key

# Personal mirror — separate encryption key
rustic init \
  --repo /mnt/mirror/personal \
  --password-file /root/keys/personal.key
```

### 4. Write the LCSAS Config

Create `/etc/lcsas/config.toml`:

```toml
[paths]
mirror_base = "/mnt/mirror"
staging     = "/mnt/staging"
database    = "/var/lib/lcsas/archive.db"

[defaults]
media_type         = "MDISC100"
ecc_redundancy_pct = 15
location           = "Home_Shelf"
optical_device     = "/dev/sr0"
label_prefix       = "ARCHIVE"
metadata_reserve_mb = 100

[repos.family]
mirror_path   = "/mnt/mirror/family"
password_file = "/root/keys/family.key"

[repos.personal]
mirror_path   = "/mnt/mirror/personal"
password_file = "/root/keys/personal.key"
```

### 5. Register Repositories

```bash
lcsas --config /etc/lcsas/config.toml \
  repo add family /mnt/mirror/family --key-file /root/keys/family.key

lcsas --config /etc/lcsas/config.toml \
  repo add personal /mnt/mirror/personal --key-file /root/keys/personal.key

# Verify
lcsas --config /etc/lcsas/config.toml repo list
```

### 6. Monthly Backup Cycle

Run this each month. Rustic's CDC (content-defined chunking) means only changed data produces new packs — even if files were moved or renamed.

```bash
# ── Step 1: Back up source directories into their Rustic mirrors ──

# Family: two directories, one encrypted repo
rustic backup \
  --repo /mnt/mirror/family \
  --password-file /root/keys/family.key \
  /srv/family1 /srv/family2

# Personal: two directories, separate key
rustic backup \
  --repo /mnt/mirror/personal \
  --password-file /root/keys/personal.key \
  /srv/personal1 /srv/personal2

# ── Step 2: Scan mirrors for new packs ──
#
# LCSAS detects new packs created by the backup and registers them
# in the archive catalog. Only genuinely new data is flagged for archival.

lcsas --config /etc/lcsas/config.toml status
# Output shows unarchived pack count per repo

# ── Step 3: Stage volumes ──
#
# Bin-packs all unarchived packs across both repos into ISOs sized
# for M-Disc. If data exceeds one disc, multiple volumes are created.
# Each ISO includes holographic catalog and RS03 ECC.
#
# Staging decouples ISO creation from burning — the machine holding
# the data need not have an optical drive.

lcsas --config /etc/lcsas/config.toml \
  stage --media MDISC100

# Output:
#   Session: 2026-02-14T19:30:00+00:00
#   Staged 2 volumes:
#     /mnt/staging/.../ARCHIVE_MD_2026_001.iso  (94.2 GB, 47 packs)
#     /mnt/staging/.../ARCHIVE_MD_2026_002.iso  (31.8 GB, 16 packs)
#   Manifest: /mnt/staging/.../session.json

# ── Optional: Remote / Deferred Burning ──
#
# If the archival machine lacks an optical drive, copy the session
# to a machine that has one:
#
#   rsync -avP /mnt/staging/<session>/ burner:/tmp/session/
#
#   # On the burner machine (no catalog DB needed):
#   lcsas burn-iso /tmp/session/ARCHIVE_MD_2026_001.iso \
#     --device /dev/sr0
#   lcsas burn-iso /tmp/session/ARCHIVE_MD_2026_002.iso \
#     --device /dev/sr0
#
#   # Sync burn receipts back to the archival machine:
#   rsync -avP burner:/tmp/session/receipts/ archiver:/tmp/receipts/
#   lcsas --config /etc/lcsas/config.toml \
#     catalog import-receipts /tmp/receipts/*.json

# ── Step 4: Burn Copy 1 (Home Shelf) ──
#
# Burns all ISOs in the current session. LCSAS prompts for disc
# insertion between volumes. The --location tag records where this
# physical copy is stored.

lcsas --config /etc/lcsas/config.toml \
  burn --session latest --location Home_Shelf

# Burns ARCHIVE_MD_2026_001.iso → [insert blank disc] → burning → done
# Burns ARCHIVE_MD_2026_002.iso → [insert blank disc] → burning → done
# Catalog updated: each pack now has 1 archived copy @ Home_Shelf.

# ── Step 5: Burn Copy 2 (Offsite Safe) ──
#
# Burns the same staged ISOs again. No re-staging, no re-scan.
# Same session, different physical location tag.

lcsas --config /etc/lcsas/config.toml \
  burn --session latest --location Offsite_Safe

# Catalog updated: each pack now has 2 archived copies.

# ── Step 6: Clean up staging ──
#
# Remove staged ISOs after all copies are burned.

lcsas --config /etc/lcsas/config.toml \
  stage --clean --session latest

# ── Step 7: Verify ──

lcsas --config /etc/lcsas/config.toml status
# All packs archived, 0 unarchived. Each pack shows 2 copies.
```

After burning, each disc contains:
- **`data/`** — encrypted pack files from both repos
- **`metadata/`** — Rustic index, keys, snapshots (per repo)
- **`catalog.db`** — complete SQLite catalog of the entire archive (holographic)
- **`volume_info.json`** — disc identity (label, UUID, media type)
- **RS03 ECC layer** — DVDisaster error correction protecting the full ISO

### Location Management

LCSAS tracks where each physical disc copy lives. Volumes can be moved
between locations, and you can sync a location to ensure it has copies
of all archived packs.

```bash
# ── List all known locations and their volume counts ──

lcsas --config /etc/lcsas/config.toml location list
# Output:
#   Home_Shelf    12 volumes, 847 packs, all current
#   Offsite_Safe  10 volumes, 803 packs, 44 packs behind

# ── Record that a disc has moved between locations ──

lcsas --config /etc/lcsas/config.toml \
  location move ARCHIVE_MD_2026_003 --from Home_Shelf --to Offsite_Safe

# ── Show which packs a location is missing ──

lcsas --config /etc/lcsas/config.toml location status Offsite_Safe
# Output:
#   Location: Offsite_Safe
#   Packs archived here: 803
#   Packs missing: 44
#     repo=family:    20 packs (2.1 GB)
#     repo=personal:  24 packs (1.8 GB)

# ── Stage & burn only the packs missing from a location ──

lcsas --config /etc/lcsas/config.toml \
  stage --media MDISC100 --for-location Offsite_Safe

lcsas --config /etc/lcsas/config.toml \
  burn --session latest --location Offsite_Safe

# ── Add a new location and bring it up to date ──

lcsas --config /etc/lcsas/config.toml location add Bank_Vault

lcsas --config /etc/lcsas/config.toml \
  stage --media MDISC100 --for-location Bank_Vault

lcsas --config /etc/lcsas/config.toml \
  burn --session latest --location Bank_Vault
```

### 7. Restoring All of Family

To recover the `family` repository (both `/srv/family1` and `/srv/family2`), you need:
1. The encryption key (`/root/keys/family.key`)
2. Any subset of discs that collectively contain all the family packs

LCSAS generates a **pick list** telling you exactly which discs to retrieve.

```bash
# ── Step 1: List available family snapshots ──

rustic snapshots \
  --repo /mnt/mirror/family \
  --password-file /root/keys/family.key

# Pick the snapshot you want (e.g., the latest: "abc123def456")

# ── Step 2: Generate a restore pick list ──
#
# This queries the archive catalog to map the snapshot's required
# packs to physical disc labels.

lcsas --config /etc/lcsas/config.toml \
  restore plan abc123def456

# Output:
#   Restore Pick List for snapshot abc123def456
#   ─────────────────────────────────────────────
#   ARCHIVE_MDISC100_0001  12 packs  (2.3 GB)
#   ARCHIVE_MDISC100_0003   8 packs  (1.7 GB)
#   ARCHIVE_MDISC100_0005   3 packs  (0.4 GB)
#   ─────────────────────────────────────────────
#   Total: 23 packs across 3 volumes
#   Missing packs: 0
#
# Either copy (Home Shelf or Offsite Safe) works — the catalog
# tracks redundant copies automatically. If a disc is damaged,
# LCSAS routes to the surviving copy.

# ── Step 3: Mount the discs and execute restore ──
#
# Insert each disc listed in the pick list. LCSAS extracts the
# needed packs into a restore cache, then runs Rustic restore
# against the assembled cache.

lcsas --config /etc/lcsas/config.toml \
  restore exec abc123def456 /srv/restored \
  --password-file /root/keys/family.key

# LCSAS will:
#   1. Prepare a restore cache with Rustic metadata
#   2. Prompt you to insert each disc from the pick list
#   3. Ingest required packs from each disc (skips packs already cached)
#   4. Run `rustic restore` against the assembled cache
#
# Result: /srv/restored/ contains the full family1 + family2 tree
# exactly as it was at snapshot abc123def456.
```

#### Restoring from Offsite Copies

If your home shelf discs are lost or damaged, the offsite copies work identically. LCSAS pick lists are aware of all volume copies — if the home copy of `ARCHIVE_MDISC100_0001` is destroyed, the offsite copy is automatically used instead.

```bash
# Mark the damaged home copy as destroyed
lcsas --config /etc/lcsas/config.toml \
  verify ARCHIVE_MDISC100_0001
# → If verification fails, the volume is flagged DEPRECATED

# Re-plan — LCSAS routes to surviving offsite copies
lcsas --config /etc/lcsas/config.toml \
  restore plan abc123def456
# Pick list now points to offsite copies instead
```

#### Restoring a Single Directory

Rustic supports path-based filters at restore time:

```bash
# Restore only /srv/family2 from the snapshot
rustic restore abc123def456 \
  --repo /path/to/restore/cache \
  --password-file /root/keys/family.key \
  --target /srv/restored \
  --filter /srv/family2
```

### Summary

| Step | Frequency | What Happens |
|------|-----------|-------------|
| Rustic backup | Monthly | CDC dedup, encrypt, write to local mirror |
| LCSAS scan | Monthly | Detect new packs, update catalog |
| LCSAS stage | Monthly | Bin-pack → stage → ISO → ECC |
| LCSAS burn (×N) | Monthly | Burn staged ISOs, once per location |
| Location sync | As needed | Stage + burn delta for a location that's fallen behind |
| Location move | As needed | Record that a disc moved between physical locations |
| Store offsite copy | Monthly | Carry discs to safe deposit / relative's house |
| LCSAS status | Anytime | Verify all packs archived, check redundancy per location |
| Restore | When needed | Pick list → mount discs → cache assembly → Rustic restore |

## Testing

Uses `TEST_TINY` (1 MB) and `TEST_SMALL` (10 MB) media types for fast pipeline tests without optical hardware. Integration tests auto-skip when external tools are not installed.

```bash
make test-unit         # Pure Python, no external deps
make test-integration  # Requires rustic, xorriso, dvdisaster
make coverage          # HTML coverage report
```
