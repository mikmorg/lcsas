# LCSAS — Linux Cold Storage Archival Suite

Petabyte-scale, offline-first archival system for Linux. Orchestrates **Rustic** (content-defined chunking), **Xorriso** (ISO mastering), and **DVDisaster** (error correction) to write deduplicated, encrypted data packs onto optical media (BD-R, M-Disc).

## Key Capabilities

- **Infinite Incrementalism** — CDC ensures file moves/renames consume zero additional storage payload
- **Multi-Tenant Isolation** — distinct datasets encrypted with different keys coexist on the same disc
- **Holographic Indexing** — every disc carries a complete SQLite catalog of the entire archive
- **Local Mirror Strategy** — permanent hot tier enables instant consolidation without retrieving offsite media
- **Bit-Rot Immunity** — DVDisaster RS03 error correction wraps every ISO image
- **Self-Contained Disaster Recovery** — meta-volume bundles portable tools + source so any disc set plus a key file restores without system dependencies

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
├── meta/         # Meta-volume builder (disaster recovery toolkit)
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

### Key Backup (Critical)

**Your encryption keys are the single point of total failure.** If the keys are
lost, all archived data is permanently unrecoverable — no matter how many disc
copies exist. Back up keys to at least two of the following:

- **Paper key** — `base64` the key file, print it, laminate, store in a safe
- **Separate USB drive** — store in a fire-rated safe or safe deposit box
- **Cryptosteel / Coldcard** — metal seed backup resistant to fire and flood
- **Trusted family member** — sealed envelope at a relative's home

Keys must **never** be stored on the same media as the archive data. The
meta-volume intentionally excludes key files.

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

#### How LCSAS Passes Keys to Rustic

Each `[repos.<name>]` block declares a `password_file` path. LCSAS reads
this path once per operation and forwards it to `rustic` via the
`--password-file` flag for every subprocess invocation (backup, scan,
restore, etc.). There is **no** `$LCSAS_PASSWORD` or `$RESTIC_PASSWORD`
environment variable: key location is config-driven so the same
`config.toml` is reproducible across machines and the CLI never accepts
secret material on the command line or via the environment.

The `password_file` value is the only supported way to tell LCSAS where
a repo's key lives. The file itself must already exist on disk and be
readable by the user running `lcsas` — see
[Key Backup (Critical)](#key-backup-critical) above for how to generate,
store, and back up the key files this field points to. Key files are
deliberately excluded from the meta-volume, so a separate backup is
mandatory.

### 5. Register Repositories

```bash
lcsas --config /etc/lcsas/config.toml \
  repo add family /mnt/mirror/family

lcsas --config /etc/lcsas/config.toml \
  repo add personal /mnt/mirror/personal

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

## Disaster Recovery (Meta-Volume)

LCSAS archive discs are encrypted and deduplicated — restoring from them requires `rustic`, `xorriso`, Python, and the LCSAS source code. If your archival machine is lost, those tools may not be available on the recovery machine.

The **meta-volume** solves this by bundling *everything* needed for restore onto a single supplementary disc burned alongside your data volumes at each storage location. The **only** thing not included is your encryption key file, which must be stored separately.

### What's on a Meta-Volume

| Path | Contents |
|------|----------|
| `tools/bin/` | Portable Linux x86_64 binaries: `rustic`, `xorriso`, `python3` |
| `tools/lib/` | All required shared libraries (discovered via `ldd`) |
| `lcsas/src/` | Complete LCSAS source code (zero pip dependencies) |
| `docs/` | Architecture documentation |
| `restore.sh` | Automated bash restore script |
| `README_RESTORE.md` | Human-readable step-by-step recovery instructions |
| `volume_info.json` | Machine-readable volume metadata |

### Building a Meta-Volume

```bash
# Build the meta-volume contents into a staging directory
lcsas meta build --output /mnt/staging/meta

# Optionally specify the project root (auto-detected by default)
lcsas meta build --output /mnt/staging/meta --project-root /opt/lcsas
```

The output directory can then be mastered to ISO and burned alongside your data volumes at each storage location.

### Restoring from Discs Only

In a disaster scenario, you have:
1. The data-volume ISOs (or physical discs)
2. The meta-volume ISO (or physical disc)
3. Your encryption key file (stored separately, e.g. in a safe)

No system-installed `rustic`, `xorriso`, or LCSAS is required.

```bash
# 1. Mount or copy the meta-volume to local disk
cp -r /media/meta-disc /tmp/lcsas-meta
cd /tmp/lcsas-meta

# 2. Copy data-volume ISOs to a directory
mkdir /tmp/isos
cp /media/disc1/*.iso /tmp/isos/
cp /media/disc2/*.iso /tmp/isos/
# ... or mount each disc and copy the ISO files

# 3. Run the bootstrap restore script
./restore.sh \
  --key ~/safe/family.key \
  --isos /tmp/isos/ \
  --target ~/restored/
```

The restore script:
1. Extracts all ISOs using the bundled `xorriso`
2. Discovers repositories from disc metadata
3. Assembles a restore cache with two-level pack layout
4. Runs `rustic restore` using the bundled `rustic` binary
5. Cleans up temporary files

#### Restore Options

| Option | Required | Description |
|--------|----------|-------------|
| `--key FILE` | Yes | Path to your encryption key file |
| `--isos DIR` | Yes | Directory containing `.iso` files |
| `--target DIR` | Yes | Where to restore files |
| `--repo NAME` | No | Restore only this repository (default: all) |
| `--snapshot ID` | No | Specific snapshot to restore (default: latest) |
| `--work-dir DIR` | No | Temporary work directory (default: auto) |

#### Advanced: Using LCSAS CLI Instead

The meta-volume also includes the full LCSAS source code, so you can use the
standard CLI for more control:

```bash
# Set up the bundled Python environment
export LD_LIBRARY_PATH=/tmp/lcsas-meta/tools/lib:$LD_LIBRARY_PATH
export PATH=/tmp/lcsas-meta/tools/bin:$PATH
export PYTHONPATH=/tmp/lcsas-meta/lcsas/src

# Use the full LCSAS restore workflow
python3 -m lcsas restore plan <snapshot-id>
python3 -m lcsas restore exec <snapshot-id> /target \
  --password-file ~/safe/family.key
```

### Supported recovery platforms

The meta-volume can carry prebuilt recovery binaries for six target
platforms.  The supported matrix (cross-platform meta-volume work
landed in Phase 21.1 — see [`docs/CROSS_PLATFORM_META_RFC.md`](docs/CROSS_PLATFORM_META_RFC.md)):

| Target | OS | Notes |
|---|---|---|
| `x86_64-unknown-linux-musl` | Linux x86_64 | static (musl), no host glibc dependency |
| `aarch64-unknown-linux-musl` | Linux ARM64 | static; covers Apple Silicon-via-Asahi, Raspberry Pi 4/5, AWS Graviton |
| `armv7-unknown-linux-gnueabihf` | Linux 32-bit ARM | Raspberry Pi 1/2/3/Zero |
| `aarch64-apple-darwin` | macOS Apple Silicon | native |
| `x86_64-apple-darwin` | macOS Intel | native |
| `x86_64-pc-windows-gnu` | Windows x86_64 | recovery via `restore.bat` |

For each target the meta-volume bundles upstream-pinned binaries:

- **Tier 2 — `rustic`** (from the
  [upstream release matrix](https://github.com/rustic-rs/rustic/releases))
- **Tier 3 — stripped CPython 3.12 interpreter** (from
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases))
  for the pure-Python recovery path

Both are pinned by SHA-256 in [`recovery/UPSTREAM.sha256`](recovery/UPSTREAM.sha256)
and downloaded with `make fetch-recovery` before building a meta-volume.
The cache lives at `~/.cache/lcsas/recovery-binaries/` (override with
`$LCSAS_RECOVERY_CACHE`) and is reused across rebuilds — the first build
is the only one that needs network.

```bash
make fetch-recovery   # one-time: ~600 MB download (6 targets × rustic + python)
lcsas meta build --output /mnt/staging/meta
```

If you skip `make fetch-recovery`, the meta-volume falls back to a
single-arch build that only contains binaries for the host architecture
(today's behavior).  Cross-arch recovery on that meta-volume requires
the recipient to install `rustic` + `python3` from the target
platform's package manager.

### Tier 1 (`lcsas-restore`) cross-platform coverage

The recovery cascade declares three tiers (see
[`recovery/docs/TIERS.txt`](recovery/docs/TIERS.txt)):

| Tier | Binary | Cross-platform today? |
|---|---|---|
| **1** (primary) | our C89 `lcsas-restore` against vendored sqlite/zstd | **All 6 approved targets** (Phase 21.10.b/.11/.12) |
| 2 (fallback) | upstream `rustic-static` | All 6 targets bundled |
| 3 (last resort) | bundled CPython + `standalone_restorer.py` | All 6 targets bundled |

The intent (see PR #30 and `recovery/docs/TIERS.txt`): tier 1 is
the **primary** recovery tool because C89 has been ABI-stable for
35 years and our binary depends only on a working libc.  Tier 2
exists as a hedge in case our binary won't run for some reason.

**Building tier-1 binaries for your meta-volume.**  The bundler
copies cross-built `lcsas-restore` binaries from
`recovery/bin/<short-arch>/` into the meta-volume's per-target
directories.  Trigger the cross-builds yourself with:

```bash
make build-recovery   # all reachable targets via `lcsas recovery build`
# or just one:
lcsas recovery build --arch x86_64           # Linux musl, native
lcsas recovery build --arch aarch64          # Linux musl, ARM64
lcsas recovery build --arch armv7            # Linux musl, 32-bit ARM (musleabihf)
lcsas recovery build --arch x86_64-windows   # Windows, via zig cc
lcsas recovery build --arch x86_64-macos     # macOS Intel,    via zig cc
lcsas recovery build --arch aarch64-macos    # macOS Apple Silicon, via zig cc
```

Then `lcsas meta build` picks them up automatically (no flags
needed — the bundler maps short-arch → rust-triple at copy time).

The default cross-compiler per Linux target is the canonical
musl-cross-make prefix (`<arch>-linux-musl-gcc` for most;
`armv7-linux-musleabihf-gcc` for the hardfloat 32-bit ARM
variant).  Override with `--cc` if your toolchain ships a
different name, or to use `zig cc`:

```bash
lcsas recovery build --arch armv7 \
    --cc "zig cc -target armv7-linux-musleabihf"
```

The Windows and macOS targets always use `zig cc` (via the
`python3 -m ziglang cc` Python wheel — `pip install ziglang` if it
isn't present).  Notably, the macOS targets do **not** require
the Apple SDK: zig bundles enough libSystem definitions to link
Mach-O executables for both Apple Silicon and Intel.  Operator
must still notarize or codesign the binary themselves if they
want macOS Gatekeeper to bless it; unsigned binaries still run
via Finder's "Open anyway" path.

On those still-pending targets the cascade falls through tier 1
(missing) → tier 2 (works) so restore succeeds; you just lose the
"C89 binary depends only on libc + kernel" durability layer.

### Decisions about coverage gaps

Targets **not** currently bundled, and the rationale (full discussion
in [`docs/CROSS_PLATFORM_META_RFC.md`](docs/CROSS_PLATFORM_META_RFC.md) §6 Q1):

- **RISC-V** — upstream rustic does not yet ship a release artifact
  for `riscv64gc-unknown-linux-gnu`; we don't cross-compile ourselves.
  Will be added when upstream ships.
- **i686** (32-bit x86) — upstream ships it but the cold-storage
  recovery audience for 32-bit x86 in 2026+ is vanishingly small.
- **FreeBSD / OpenBSD** — no upstream rustic.  Recipient must install
  `rustic` from ports.
- **Windows ARM64** — no upstream rustic.  Recipient must install
  `rustic` via winget or similar.

The recovery driver (`recovery/scripts/restore.sh`) auto-detects the
recovery host's `(uname -s, uname -m)` and selects the right
`bin/<target>/` subtree.  Override with `$LCSAS_TARGET=<target-triple>`
if auto-detection misfires (e.g. when running under QEMU or chroot).

### Operational Recommendation

Rebuild and burn an updated meta-volume whenever you upgrade LCSAS or system tools. Include one meta-volume at **every** storage location so that any single location's discs plus the key file are sufficient for full recovery.

```bash
# Typical burn cycle: data volumes + meta-volume
lcsas stage --media MDISC100
lcsas meta build --output /mnt/staging/meta
# Master meta/ to ISO, then burn both data and meta ISOs
lcsas burn --session latest --location Home_Shelf
lcsas burn --session latest --location Offsite_Safe
```

## Testing

Uses the `TEST_TINY` (1 MB) media type for fast pipeline tests without optical hardware. Integration tests auto-skip when external tools are not installed.

```bash
make test-unit         # Pure Python, no external deps
make test-integration  # Requires rustic, xorriso, dvdisaster
make coverage          # HTML coverage report
```

## Development

LCSAS shells out to three external binaries: **rustic**, **xorriso**, and **dvdisaster**. The integration test suite exercises real subprocess calls against these tools (no Protocol mocks), so you need them on your `PATH` before running `make test-integration`. CI runs the same versions documented here.

### Pinned tool versions

| Tool        | Version pinned in CI | Notes |
|-------------|----------------------|-------|
| rustic      | **v0.11.2**          | matches `.github/workflows/test.yml` |
| xorriso     | distro-provided      | any recent version is fine |
| dvdisaster  | distro-provided      | RS03 encoder must be present |
| cdemu       | distro-provided      | optional, only for the e2e blind-restore suite |

### Install rustic

The CI workflow downloads the official release tarball from `rustic-rs/rustic` and drops the binary into `$HOME/.local/bin`. Mirror that locally:

```bash
# Linux x86_64 — same artifact CI uses
VERSION=0.11.2
ASSET="rustic-v${VERSION}-x86_64-unknown-linux-gnu.tar.gz"
SHA256="fb7b74a14418b2dd070360c9abb22607f8559bdd344a0adf1b33bc2e31f83f5f"
curl -fsSL -O "https://github.com/rustic-rs/rustic/releases/download/v${VERSION}/${ASSET}"
echo "${SHA256}  ${ASSET}" | sha256sum -c -
tar -xzf "${ASSET}"
install -m 0755 rustic "$HOME/.local/bin/rustic"
rustic --version
```

Alternatively, build from source with Cargo (adds a ~5 minute Rust toolchain step but works on any architecture):

```bash
cargo install --locked --version 0.11.2 rustic-backup
```

The Cargo crate is named `rustic-backup` (the `rustic` crate name is taken). It installs an executable called `rustic`.

### Install xorriso

```bash
# Debian / Ubuntu
sudo apt-get install -y xorriso

# Fedora / RHEL
sudo dnf install -y xorriso

# macOS (Homebrew)
brew install xorriso
```

### Install dvdisaster

```bash
# Debian / Ubuntu
sudo apt-get install -y dvdisaster

# Fedora / RHEL — not in default repos; build from source:
#   https://dvdisaster.jcea.es/

# macOS — no official Homebrew formula. Build from source or run inside a
# Linux container/VM. The integration suite auto-skips when dvdisaster is
# missing.
```

### Install cdemu (optional — e2e blind-restore only)

The `make blind-restore` target uses cdemu to expose ISO files as virtual optical drives. It requires the **vhba** kernel module, which is not available on most CI runners (including GitHub Actions) — that's why the CI workflow does **not** run the e2e suite. To run it locally on Linux:

```bash
sudo apt-get install -y cdemu-client cdemu-daemon vhba-dkms
sudo modprobe vhba
sudo make blind-restore
```

On macOS / non-Linux hosts the blind-restore suite is unsupported; rely on `make test-integration` instead.

### Continuous integration

`.github/workflows/test.yml` runs on every push and pull request:

1. Installs rustic from the pinned release tarball above
2. `apt-get install`s xorriso + dvdisaster
3. `make dev` → `make test-unit` → `make test-integration` → `make typecheck` → `make lint`

The e2e blind-restore suite is intentionally excluded from CI (cdemu/vhba unavailable on GitHub runners). Run it locally before cutting a release.
