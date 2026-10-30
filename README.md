# LCSAS — Linux Cold Storage Archival Suite

Durable, offline-first cold archival for Linux — built so data stays restorable for decades, by a non-specialist, with no cloud provider and no central server. Orchestrates **Rustic** (content-defined chunking), **Xorriso** (ISO mastering), and **DVDisaster** (error correction) to write deduplicated, encrypted data packs onto optical discs (BD-R, M-Disc). Scales to archives of hundreds of discs across multiple repositories.

> **🚨 In an emergency and need to restore data?**
> Start at **[docs/RECOVERY_GUIDE.md](docs/RECOVERY_GUIDE.md)** — it
> routes you to the right OS-specific walkthrough (Linux, macOS,
> Windows, bare-metal, or disc-only Python reader) in one click.

## Key Capabilities

- **Infinite Incrementalism** — CDC ensures file moves/renames consume zero additional storage payload
- **Per-Repository Isolation** — repositories, each encrypted under its own password, share the same discs
- **A Catalog on Every Disc** — every disc carries a disc catalog: a SQLite copy of the catalog, current as of that disc's burn
- **Local Mirror Strategy** — the mirror stays on local disk, so consolidation needs no offsite discs
- **Bit-Rot Immunity** — DVDisaster RS03 error correction wraps every ISO image
- **Self-Contained Restore** — the meta disc bundles portable tools + source so the discs plus the password are enough to restore, with no system dependencies

## Quick Start

```bash
# Install in development mode
make dev

# Run unit tests (no external tools required)
make test-unit

# Run all tests (requires rustic, xorriso, dvdisaster)
make test-all
```

## Documentation

Start at the **[documentation index](docs/README.md)** — it routes to the
architecture reference, on-disc formats, owner and heir guides, the workflow
matrix, and the on-disc recovery manuals.

The full synthesized architecture reference lives at
[docs/architecture.md](docs/architecture.md).

## No Independent Security Review

LCSAS v1.0 ships without an independent security review. No outside person
has reviewed LCSAS's own code, including the code that handles the crypto:
the tier-1 C reader's decryption and authentication (scrypt, AES, Poly1305),
the tier-3 Python reader's decryption and authentication, and the SLIP-0039
share code (in Python and in the `lcsas-keyshare` C binary). The 2026-06
audit was self-graded. The encryption format itself is not LCSAS's: it is
restic's repository format, which rustic also uses.

Why this is acceptable:

- **A reader bug fails loudly.** Every blob is authenticated (a Poly1305
  MAC and a SHA-256 hash), so a bug in a reader's decryption or
  authentication makes the restore reject data instead of restoring wrong
  data. Tier 2 (rustic) and tier 2b (stock restic) share none of the
  readers' code.
- **Tier 2b is the hedge.** It restores with no LCSAS code at all. The steps
  are in [`recovery/docs/RESTORE_STANDARD_TOOLS.txt`](recovery/docs/RESTORE_STANDARD_TOOLS.txt).
- **The silent risk is the share code.** A bug there could write share cards
  that do not rebuild the password. The yearly `lcsas key verify` in the
  [owner schedule](docs/workflows/verify-and-audit.md#owner-schedule) tests
  exactly that: it rebuilds the password from the cards and checks that it
  unlocks the repository.

## Project Structure

```
src/lcsas/
├── cli/          # argparse CLI interface (22 top-level subcommands)
├── config/       # Settings, media types, TOML config loading
├── db/           # SQLite schema (v9), connection, CRUD, queries
├── rustic/       # Rustic subprocess wrapper (Protocol-based)
├── packs/        # Pack scanner and delta analysis
├── binpack/      # First-fit-decreasing bin packing algorithm
├── staging/      # Staging-area builder, disc catalog + repository metadata
├── iso/          # Xorriso ISO creation wrapper
├── ecc/          # DVDisaster ECC wrapper
├── burn/         # Burn orchestrator (full pipeline)
├── restore/      # Restore planner, executor, Python reader (tier 3)
├── consolidate/  # Volume merger / consolidation
├── meta/         # Meta disc builder (recovery tools)
├── recovery/     # C89 recovery-binary cross-compilation harness (tier-1)
├── keyshare/     # Pure-Python SLIP-0039 Shamir password splitting
└── utils/        # Hashing, filesystem helpers, label generation
```

## Usage Guide

This walkthrough covers a realistic home archival scenario: two family members and two personal directories, backed up onto M-Disc with two copies of every volume (one disc at home, one offsite), with monthly incrementals.

### Scenario

| Directory | Repository | Password File |
|-----------|-----------|----------------|
| `/srv/family1` | `family` | `/root/keys/family.key` |
| `/srv/family2` | `family` | `/root/keys/family.key` |
| `/srv/personal1` | `personal` | `/root/keys/personal.key` |
| `/srv/personal2` | `personal` | `/root/keys/personal.key` |

Both repositories write to 100 GB M-Discs. Each burn cycle produces two discs of every volume — one for the home shelf, one for an offsite safe.

### 1. Install and Initialize

```bash
# Install LCSAS
pip install -e .

# Initialize the master catalog
lcsas init --db-path /var/lib/lcsas/archive.db
```

### 2. Create Password Files

Each repository gets its own password so that compromise of one password does not expose the other repository.

```bash
# Generate a separate password file for each repository
mkdir -p /root/keys
dd if=/dev/urandom bs=64 count=1 2>/dev/null | base64 > /root/keys/family.key
dd if=/dev/urandom bs=64 count=1 2>/dev/null | base64 > /root/keys/personal.key
chmod 600 /root/keys/*.key
```

### Password Backup (Critical)

**Your passwords are the single point of total failure.** If the passwords are
lost, all archived data can never be restored — no matter how many discs
exist. Back up each password to at least two of the following:

- **Paper copy** — `base64` the password file, print it, laminate, store in a safe
- **Separate USB drive** — store in a fire-rated safe or safe deposit box
- **Cryptosteel / Coldcard** — metal seed backup resistant to fire and flood
- **Trusted family member** — sealed envelope at a relative's home

Passwords must **never** be stored on the same discs as the archive data. The
meta disc intentionally excludes password files.

### 3. Initialize Rustic Repositories (Local Mirrors)

LCSAS uses Rustic to create deduplicated, encrypted local mirrors. Each mirror is your permanent live copy of a repository and stays on your server.

```bash
# Family mirror — one repository backing up two source directories
rustic init \
  --repo /mnt/mirror/family \
  --password-file /root/keys/family.key

# Personal mirror — separate password file
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
# Deprecated/ignored: RS03 augmented images pad to the smallest fitting
# medium; the padding is the effective redundancy (non-15 values warn).
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

#### How LCSAS Passes Passwords to Rustic

Each `[repos.<name>]` block declares a `password_file` path. LCSAS reads
this path once per operation and forwards it to `rustic` via the
`--password-file` flag for every subprocess invocation (backup, scan,
restore, etc.). The archival CLI honors **no** `$LCSAS_PASSWORD` or
`$RESTIC_PASSWORD` environment variable: where each password file lives
is set in the config, so the same `config.toml` is reproducible across
machines and the CLI never accepts secret material on the command line
or via the environment. (The restore script on the meta disc is the
one exception — `restore.sh` accepts `LCSAS_PASSWORD` to skip its
interactive prompt; see `recovery/docs/ENV_VARS.txt`.)

The `password_file` value is the only supported way to tell LCSAS where
a repository's password file lives. The file itself must already exist
on disk and be readable by the user running `lcsas` — see
[Password Backup (Critical)](#password-backup-critical) above for how
to generate, store, and back up the password files this field points
to. Password files are deliberately excluded from the meta disc, so a
separate backup is mandatory.

### 5. Register Repositories

```bash
lcsas --config /etc/lcsas/config.toml \
  repo add family /mnt/mirror/family

lcsas --config /etc/lcsas/config.toml \
  repo add personal /mnt/mirror/personal

# Check
lcsas --config /etc/lcsas/config.toml repo list
```

### 6. Monthly Backup Cycle

Run this each month. Rustic's CDC (content-defined chunking) means only changed data produces new packs — even if files were moved or renamed.

```bash
# ── Step 1: Back up source directories into their Rustic mirrors ──

# Family: two directories, one encrypted repository
rustic backup \
  --repo /mnt/mirror/family \
  --password-file /root/keys/family.key \
  /srv/family1 /srv/family2

# Personal: two directories, separate password file
rustic backup \
  --repo /mnt/mirror/personal \
  --password-file /root/keys/personal.key \
  /srv/personal1 /srv/personal2

# ── Step 2: Scan mirrors for new packs ──
#
# LCSAS detects new packs created by the backup and registers them
# in the master catalog. Only genuinely new data is flagged for archival.

lcsas --config /etc/lcsas/config.toml scan

lcsas --config /etc/lcsas/config.toml status
# Output shows unarchived pack count per repository

# ── Step 3: Stage volumes ──
#
# Bin-packs all unarchived packs across both repositories into ISO images
# sized for M-Disc. If data exceeds one disc, multiple volumes are created.
# Each ISO image includes the disc catalog and RS03 ECC.
#
# Staging decouples ISO creation from burning — the machine holding
# the data need not have an optical drive.

lcsas --config /etc/lcsas/config.toml \
  stage --media MDISC100

# Output:
#   Session: 2026-02-14T19:30:00+00:00
#   Staged 2 volumes:
#     /mnt/staging/.../ARCHIVE_MD100_2026_0001.iso  (94.2 GB, 47 packs)
#     /mnt/staging/.../ARCHIVE_MD100_2026_0002.iso  (31.8 GB, 16 packs)
#   Manifest: /mnt/staging/.../session.json

# ── Optional: Remote / Deferred Burning ──
#
# If the archival machine lacks an optical drive, copy the burn session
# to a machine that has one:
#
#   rsync -avP /mnt/staging/<session>/ burner:/tmp/session/
#
#   # On the burner machine (no catalog needed). --emit-receipt
#   # writes a burn-receipt JSON; --location is required with it:
#   lcsas burn-iso /tmp/session/ARCHIVE_MD100_2026_0001.iso \
#     --device /dev/sr0 \
#     --emit-receipt /tmp/session/receipts/ --location Home_Shelf
#   lcsas burn-iso /tmp/session/ARCHIVE_MD100_2026_0002.iso \
#     --device /dev/sr0 \
#     --emit-receipt /tmp/session/receipts/ --location Home_Shelf
#
#   # Sync burn receipts back to the archival machine:
#   rsync -avP burner:/tmp/session/receipts/ archiver:/tmp/receipts/
#   lcsas --config /etc/lcsas/config.toml \
#     catalog import-receipts /tmp/receipts/*.json

# ── Step 4: Burn the Home Shelf discs ──
#
# Burns all ISO images in the current burn session. LCSAS prompts for
# disc insertion between volumes. The --location tag records where
# these discs are kept. The staged ISO images persist after burning —
# they are kept until Step 6 so more discs can be burned without
# re-staging.

lcsas --config /etc/lcsas/config.toml \
  burn --session latest --location Home_Shelf

# Burns ARCHIVE_MD100_2026_0001.iso → [insert blank disc] → burning → done
# Burns ARCHIVE_MD100_2026_0002.iso → [insert blank disc] → burning → done
# Catalog updated: each pack now has 1 archived copy @ Home_Shelf.

# ── Step 5: Burn the Offsite Safe discs ──
#
# Burns the same staged ISO images again. No re-staging, no re-scan.
# Same burn session, different location tag.

lcsas --config /etc/lcsas/config.toml \
  burn --session latest --location Offsite_Safe

# Catalog updated: each pack now has 2 archived copies.

# ── Step 6: Clean up the staging area ──
#
# Remove staged ISO images after all discs are burned.

lcsas --config /etc/lcsas/config.toml \
  stage --clean --session latest

# ── Step 7: Check status ──

lcsas --config /etc/lcsas/config.toml status
# All packs archived, 0 unarchived. Each pack shows 2 copies.
```

After burning, each disc contains:
- **`data/`** — encrypted pack files from both repositories
- **`metadata/`** — repository metadata: Rustic index, keys, snapshots (per repository)
- **`catalog.db`** — the disc catalog: a SQLite copy of the catalog, current as of this disc's burn
- **`volume_info.json`** — volume identity (label, UUID, media type)
- **`VOLUME_INFO.txt`** — the same in plain text for an heir: label, place in its burn session, when it was prepared, LCSAS version, repositories, pack count and size
- **RS03 ECC layer** — DVDisaster error correction protecting the full ISO image

### Location Management

LCSAS tracks where each disc is kept. Discs can be moved between
locations, and you can sync a location to make sure its discs hold
every archived pack.

```bash
# ── List all known locations and their volume counts ──

lcsas --config /etc/lcsas/config.toml location list
# Output:
#   Home_Shelf    12 volumes, 847 packs, all current
#   Offsite_Safe  10 volumes, 803 packs, 44 packs behind

# ── Record that a disc has moved between locations ──

lcsas --config /etc/lcsas/config.toml \
  location move ARCHIVE_MD100_2026_0003 --from Home_Shelf --to Offsite_Safe

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

To restore the `family` repository (both `/srv/family1` and `/srv/family2`), you need:
1. The password file (`/root/keys/family.key`)
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
# This queries the master catalog to map the snapshot's required
# packs to disc labels.

lcsas --config /etc/lcsas/config.toml \
  restore plan abc123def456 --repo family

# Output:
#   Restore Pick List for snapshot abc123def456
#   ─────────────────────────────────────────────
#   ARCHIVE_MD100_2026_0001  12 packs  (2.3 GB)
#   ARCHIVE_MD100_2026_0003   8 packs  (1.7 GB)
#   ARCHIVE_MD100_2026_0005   3 packs  (0.4 GB)
#   ─────────────────────────────────────────────
#   Total: 23 packs across 3 volumes
#   Missing packs: 0
#
# Either disc (Home Shelf or Offsite Safe) works — the catalog
# tracks every disc of each volume. If a disc is damaged,
# LCSAS routes to a surviving disc.

# ── Step 3: Mount the discs and execute restore ──
#
# Insert each disc listed in the pick list. LCSAS extracts the
# needed packs into a restore cache, then runs Rustic restore
# against the assembled cache.

lcsas --config /etc/lcsas/config.toml \
  restore exec abc123def456 /srv/restored \
  --repo family --password-file /root/keys/family.key

# LCSAS will:
#   1. Prepare a restore cache with the repository metadata
#   2. Prompt you to insert each disc from the pick list
#   3. Ingest required packs from each disc (skips packs already cached)
#   4. Run `rustic restore` against the assembled cache
#
# Result: /srv/restored/ contains the full family1 + family2 tree
# exactly as it was at snapshot abc123def456.
```

#### Restoring from Offsite Discs

If your home shelf discs are lost or damaged, the offsite discs work identically. LCSAS pick lists know about every disc of each volume — if the home disc of `ARCHIVE_MD100_2026_0001` is destroyed, the offsite disc is automatically used instead.

```bash
# Check the damaged home disc
lcsas --config /etc/lcsas/config.toml \
  verify ARCHIVE_MD100_2026_0001
# → If verification fails, a VERIFY_FAIL event is recorded in the
#   volume's audit trail. Taking the disc out of service is a
#   separate, explicit step:
lcsas --config /etc/lcsas/config.toml \
  copy deprecate ARCHIVE_MD100_2026_0001 Home_Shelf

# Re-plan — LCSAS routes to surviving offsite discs
lcsas --config /etc/lcsas/config.toml \
  restore plan abc123def456 --repo family
# Pick list now points to offsite discs instead
```

Record a disc you destroyed with `copy destroy`, and one you cannot find with `copy destroy --lost`: both get the disc status `DESTROYED`, and the volume's events say which. A lost disc may be in a stranger's hands, so the command reminds you that it carries the disc catalog and the locked key files (see [docs/DISC_CONFIDENTIALITY.md](docs/DISC_CONFIDENTIALITY.md)). When the last in-service disc of a volume goes, the volume is lost: `lcsas status` shows it as `LOST` (the catalog records `DEPRECATED`), and the command lists the packs now only in the mirror, which the next `lcsas stage` burns again, and, loudly, any pack that was also pruned from the mirror, which neither the mirror nor any disc in service holds any more.

```bash
lcsas --config /etc/lcsas/config.toml \
  copy destroy --lost ARCHIVE_MD100_2026_0001 Offsite_Safe
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
| Location move | As needed | Record that a disc moved between locations |
| Store offsite discs | Monthly | Carry discs to safe deposit / relative's house |
| LCSAS status | Anytime | Check all packs archived, check redundancy per location |
| Disc checks | See the [owner schedule](docs/workflows/verify-and-audit.md#owner-schedule) | Read discs back, re-burn on any read error, test restore, yearly drill |
| Restore | When needed | Pick list → mount discs → cache assembly → Rustic restore |

## Restoring After a Disaster (Meta Disc)

LCSAS archive discs are encrypted and deduplicated — restoring from them requires `rustic`, `xorriso`, Python, and the LCSAS source code. If your archival machine is lost, those tools may not be available on the machine you restore on.

The **meta disc** solves this by bundling *everything* needed for a restore onto a single extra disc, burned alongside your data volumes and kept at each location. The **only** thing not included is your password, which must be stored separately.

### What's on a Meta Disc

| Path | Contents |
|------|----------|
| `tools/bin/<target>/` | Per-platform tier-1 (`lcsas-restore`) + tier-2 (`rustic-static`) binaries for six targets: Linux x86_64/aarch64/armv7 musl, macOS Intel + Apple Silicon, Windows x86_64-gnu.  See [`docs/CROSS_PLATFORM_META_RFC.md`](docs/CROSS_PLATFORM_META_RFC.md). |
| `tools/lib/` | Shared libraries for the bundled CPython interpreter (tier-3 fallback) |
| `lcsas/src/` | Complete LCSAS source code (zero pip dependencies) |
| `recovery/docs/` | On-disc recovery manual (`RECOVER.txt`, `TIERS.txt`, `ENV_VARS.txt`, …) |
| `restore.sh` | Interactive restore script — start here |
| `restore.bat` | Windows entry point |
| `recovery/scripts/restore-auto.sh` | Non-interactive variant for automation (off the disc root, so heirs see one restore script) |
| `recovery/scripts/restore_legacy.sh` | Superseded flag-style bash driver, kept for compatibility |
| `standalone_restorer.py` | Tier-3 Python reader (requires only Python 3.10+) |
| `README_RESTORE.md` | Human-readable step-by-step restore instructions |
| `VOLUME_INFO.txt` | Plain-text note: this is the meta disc, when it was built, the LCSAS version |
| `volume_info.json` | Machine-readable details of this disc |

### Building a Meta Disc

```bash
# Build the meta disc contents into a directory in the staging area
lcsas meta build --output /mnt/staging/meta

# Optionally specify the project root (auto-detected by default)
lcsas meta build --output /mnt/staging/meta --project-root /opt/lcsas
```

The output directory can then be mastered to an ISO image and burned alongside your data volumes, with one meta disc kept at each location.

### Restoring from Discs Only

In a disaster scenario, you have:
1. The data discs (or their ISO images)
2. The meta disc (or its ISO image)
3. Your password (stored separately, e.g. in a safe)

No system-installed `rustic`, `xorriso`, or LCSAS is required — everything is
on the meta disc.

#### Physical Disc Swap (the normal path)

Insert the meta disc, mount it, and run `restore.sh` directly.  The script
prompts you interactively and pauses whenever it needs the next data disc.

```bash
# 1. Mount the meta disc
sudo mount /dev/sr0 /mnt

# 2. Start the restore — it will ask for your password, then prompt
#    for each data disc as it needs them
sh /mnt/restore.sh ~/restored/ latest

# With multiple repositories on the archive the script prints a numbered menu:
#   Choose a repository (number or name): <pick one and press Enter>
# (single-repository archives skip the prompt), then:
#   Password:   <type your password and press Enter>
#
# When a data disc is needed it prints a swap prompt:
#   Insert the right disc and press ENTER to retry.
#
# Eject the current disc and insert the requested data disc.  Once it
# is mounted the restore carries on by itself (Linux/macOS; press Enter
# if it doesn't, or on Windows).  Repeat until it prints
# "restore complete".
```

See [`recovery/docs/RECOVER.txt`](recovery/docs/RECOVER.txt) for the
complete restore guide, including the disc swap steps and all
environment variables.

#### Restoring from ISO Images (no optical drive)

If you have ISO images on disk rather than physical discs, you can
loop-mount each one as needed.  The workflow is identical to the physical
path — `restore.sh` uses `/dev/sr0` (or whatever device you mount the
ISO on), so mount each ISO in turn when the swap prompt appears:

```bash
# Mount the meta ISO
sudo mount -o loop lcsas_meta.iso /mnt
sh /mnt/restore.sh ~/restored/ latest

# When a swap prompt appears, mount the next data ISO:
sudo umount /mnt
sudo mount -o loop lcsas_data_0001.iso /mnt
# press Enter at the swap prompt

# Repeat for each data ISO as prompted.
```

### Supported recovery platforms

The meta disc can carry prebuilt recovery binaries for six target
platforms.  The supported matrix (cross-platform meta disc work
landed across Phase 21.1–21.12 — see [`docs/CROSS_PLATFORM_META_RFC.md`](docs/CROSS_PLATFORM_META_RFC.md)):

| Target | OS | Notes |
|---|---|---|
| `x86_64-unknown-linux-musl` | Linux x86_64 | static (musl), no host glibc dependency |
| `aarch64-unknown-linux-musl` | Linux ARM64 | static; covers Apple Silicon-via-Asahi, Raspberry Pi 4/5, AWS Graviton |
| `armv7-unknown-linux-gnueabihf` | Linux 32-bit ARM | Raspberry Pi 1/2/3/Zero |
| `aarch64-apple-darwin` | macOS Apple Silicon | native |
| `x86_64-apple-darwin` | macOS Intel | native |
| `x86_64-pc-windows-gnu` | Windows x86_64 | restore via `restore.bat` |

For each target the meta disc bundles upstream-pinned binaries:

- **Tier 2 — `rustic`** (from the
  [upstream release matrix](https://github.com/rustic-rs/rustic/releases))
- **Tier 3 — stripped CPython 3.12 interpreter** (from
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases))
  for the Python reader

Both are pinned by SHA-256 in [`recovery/UPSTREAM.sha256`](recovery/UPSTREAM.sha256)
and downloaded with `make fetch-recovery` before building a meta disc.
The cache lives at `~/.cache/lcsas/recovery-binaries/` (override with
`$LCSAS_RECOVERY_CACHE`) and is reused across rebuilds — the first build
is the only one that needs network.

```bash
make fetch-recovery   # one-time: ~600 MB download (6 targets × rustic + python)
lcsas meta build --output /mnt/staging/meta
```

If you skip `make fetch-recovery`, the meta disc falls back to a
single-arch build that only contains binaries for the host architecture
(today's behavior).  A cross-arch restore from that meta disc requires
the heir to install `rustic` + `python3` from the target
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

**Building tier-1 binaries for your meta disc.**  The bundler
copies cross-built `lcsas-restore` binaries from
`recovery/bin/<short-arch>/` into the meta disc's per-target
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
Mach-O executables for both Apple Silicon and Intel.  The owner
must still notarize or codesign the binary themselves if they
want macOS Gatekeeper to bless it; unsigned binaries still run
via Finder's "Open anyway" path.

All six approved targets now ship a tier-1 `lcsas-restore` binary
(Phase 21.12), so the C89 durability layer is present on each.  For a
target that is **not** bundled (the coverage gaps below), the cascade
falls through tier 1 (missing) → tier 2 (works) so restore still
succeeds; you just lose the "C89 binary depends only on libc + kernel"
durability layer.

### Decisions about coverage gaps

Targets **not** currently bundled, and the rationale (full discussion
in [`docs/CROSS_PLATFORM_META_RFC.md`](docs/CROSS_PLATFORM_META_RFC.md) §6 Q1):

- **RISC-V** — upstream rustic (tier 2) does not yet ship a release
  artifact for `riscv64gc-unknown-linux-gnu`, and we don't cross-compile
  rustic ourselves; will be added when upstream ships. (The tier-1
  `lcsas-restore` CLI can already cross-build for `riscv64` —
  `lcsas recovery build --arch riscv64` — but riscv64 is not one of the
  six approved bundled targets.)
- **i686** (32-bit x86) — upstream ships it but the number of heirs
  restoring on 32-bit x86 in 2026+ is vanishingly small.
- **FreeBSD / OpenBSD** — no upstream rustic.  The heir must install
  `rustic` from ports.
- **Windows ARM64** — no upstream rustic.  The heir must install
  `rustic` via winget or similar.

The restore driver (`recovery/scripts/restore.sh`) auto-detects the
restoring host's `(uname -s, uname -m)` and selects the right
`bin/<target>/` subtree.  Override with `$LCSAS_TARGET=<target-triple>`
if auto-detection misfires (e.g. when running under QEMU or chroot).

### Operational Recommendation

Rebuild and burn an updated meta disc whenever you upgrade LCSAS or system tools. Keep one meta disc at **every** location so that any single location's discs plus the password are enough for a full restore.

```bash
# Typical burn cycle: data volumes + meta disc
lcsas stage --media MDISC100
lcsas meta build --output /mnt/staging/meta
# Master meta/ to ISO, then burn both data and meta ISOs
lcsas burn --session latest --location Home_Shelf
lcsas burn --session latest --location Offsite_Safe
```

## Testing

Uses the `TEST_TINY` (2 MB) media type for fast pipeline tests without optical hardware. Integration tests auto-skip when external tools are not installed.

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

### Running on a host without Python 3.11+ (Docker)

LCSAS requires Python ≥ 3.11 (`tomllib`, `StrEnum`), which rules out older
distributions — Debian 10 ships Python 3.7. `docker/Dockerfile.host-a` packages
the assembly half of the pipeline so such a host can still scan, bin-pack,
stage, master ISOs, and apply ECC. Debian 12 supplies every external tool at or
above its enforced floor (python 3.11.2, xorriso 1.5.4, dvdisaster 0.79.5), so
nothing is built from source; only rustic is pinned, to the same v0.11.2 musl
artifact listed in `recovery/UPSTREAM.sha256` as the tier-2 recovery reader.

The image is **host A only** — it never touches optical hardware, so it needs no
device passthrough and runs unprivileged. Burning happens on the host with the
drive via `lcsas burn-iso`, which requires no catalog and no mirror. See
[docs/workflows/burn-iso-portable.md](docs/workflows/burn-iso-portable.md).

```bash
docker build -t lcsas:host-a -f docker/Dockerfile.host-a .
```

Two wrappers ship alongside it — symlink them onto `PATH`:

| Script | Runs |
|---|---|
| `docker/lcsas` | LCSAS itself; auto-injects `--config` (the CLI has no default) |
| `docker/rustic-c` | the image's pinned rustic, so the mirror is written by the same build the recovery cascade reads |

```bash
ln -s "$PWD/docker/lcsas"    ~/bin/lcsas
ln -s "$PWD/docker/rustic-c" ~/bin/rustic-c

lcsas scan
lcsas stage --media CD700
rustic-c -r /srv/lcsas/mirror/personal \
    --password-file /srv/lcsas/keys/personal.key backup /srv/data/personal
```

Both mount `LCSAS_ROOT` (default `/srv/lcsas`) as a **single** bind mount, because
staging hardlinks packs out of the mirror and falls back to a full copy on
`EXDEV` — two separate mounts read as two devices and silently copy every
volume in full. They also run as the invoking user, so nothing is left
root-owned, and pin the container cwd to `LCSAS_ROOT` so relative paths
resolve somewhere predictable. Override with `LCSAS_IMAGE`, `LCSAS_ROOT`,
`LCSAS_CONFIG`, `LCSAS_SRC` (rustic-c source mounts, read-only) and
`LCSAS_EXTRA` (lcsas restore targets).

`[paths].database` may point anywhere under `LCSAS_ROOT`; every command
honours it, and `LCSAS_CONFIG` is injected automatically.

The image bakes in `src/`, so rebuild it after changing LCSAS source.

On a pre-bullseye Docker host, bookworm's glibc can trip an old `libseccomp2`
under Docker's default seccomp profile; install `libseccomp2` ≥ 2.4.4 from
backports on the host if containers fail in unexplained ways.

### Continuous integration

`.github/workflows/test.yml` runs on every push and pull request:

1. Installs rustic from the pinned release tarball above
2. `apt-get install`s xorriso + dvdisaster
3. `make dev` → `make -C recovery all test` (C smoke: build every recovery
   binary + run the C unit tests) → `make test-unit` →
   `make test-integration` → e2e pipeline test → `make typecheck` →
   `make lint`
4. A second `recovery-hardening` job runs the recovery-hardening suite,
   including `make shell-coverage`

The e2e blind-restore suite is intentionally excluded from CI (cdemu/vhba unavailable on GitHub runners). Run it locally before cutting a release.
