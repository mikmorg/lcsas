# Disc-Only Pure-Python Restore (Tier 5)

> Recovery tier 5 — the fallback of last resort. A single LCSAS data disc,
> a Python 3.10+ interpreter, the encryption key, and **nothing else**.
> No meta-disc. No network. No `rustic`/`restic` binary. No installed
> `lcsas` package. No cross-disc reconstruction.

This document is the doomsday playbook that proves the project's
"zero runtime dependencies" claim
(`CLAUDE.md` — *Key design patterns / Zero runtime dependencies*). Every
LCSAS data disc carries a self-contained pure-Python restorer
(`standalone_restorer.py`) that reads encrypted restic packs using only
the standard library, plus the holographic SQLite catalog that lets the
disc identify itself without any external database.

Sibling recovery tiers (more capable, prefer them when available):

- `docs/workflows/restore-host-linux.md` — full LCSAS install on Linux
- `docs/workflows/restore-bare-metal.md` — boot the meta-volume
- `docs/workflows/restore-windows.md` — Windows host with rustic.exe
- `docs/workflows/meta-volume.md` — bootable disaster-recovery disc

---

## Table of contents

1. [When to use Tier 5](#when-to-use-tier-5)
2. [What every data disc contains](#what-every-data-disc-contains)
3. [Workflow A — Mount a single data disc anywhere Python runs](#workflow-a--mount-a-single-data-disc-anywhere-python-runs)
4. [Workflow B — Run `standalone_restorer.py` from the disc](#workflow-b--run-standalone_restorerpy-from-the-disc)
5. [Workflow C — `lcsas restore from-disc` (convenience wrapper)](#workflow-c--lcsas-restore-from-disc-convenience-wrapper)
6. [Workflow D — Use the holographic SQLite catalog](#workflow-d--use-the-holographic-sqlite-catalog)
7. [Workflow E — AES-256-CTR + Poly1305 + zstd decrypt path](#workflow-e--aes-256-ctr--poly1305--zstd-decrypt-path)
8. [Workflow F — Recover a single file vs a full snapshot](#workflow-f--recover-a-single-file-vs-a-full-snapshot)
9. [Hard limits of Tier 5](#hard-limits-of-tier-5)
10. [Test coverage matrix](#test-coverage-matrix)
11. [Consolidated source refs](#consolidated-source-refs)

---

## When to use Tier 5

Use this path **only** if all higher tiers are unreachable. Tier 5
operates at ~1 MB/s
(`src/lcsas/restore/restic_fallback.py:10`) and **cannot** stitch packs
together across multiple discs (`src/lcsas/restore/restic_fallback.py:539`).

Pick Tier 5 when, and only when:

- No working `rustic` or `restic` binary is available for the host
  architecture (`src/lcsas/cli/main.py:2216`).
- The meta-volume is lost, damaged, or its bundled binaries cannot
  execute (different CPU, glibc ABI break).
- Internet access is unavailable, so a new binary cannot be downloaded.
- You hold **at least one** data disc (and the encryption key) whose
  contents include the snapshot you want.

If the snapshot you want spans multiple discs and rustic is unavailable,
you must extract pack files from every relevant disc into a single
cache directory **before** invoking the restorer
(`src/lcsas/cli/main.py:2255`–`2261`). The restorer itself does no
disc-prompting; that orchestration lives in the LCSAS wrapper, not in
the standalone script.

---

## What every data disc contains

The `HolographicInjector` ensures every disc is self-describing
(`src/lcsas/staging/metadata.py:28`). After
`HolographicInjector.write_*` runs during the burn pipeline, the root
of each ISO contains:

| Path on disc | Source | Purpose for Tier 5 |
| --- | --- | --- |
| `data/` | hardlinked packs (`staging/builder.py`) | The encrypted restic pack files. Flat layout, one file per SHA-256 (`src/lcsas/restore/restic_fallback.py:549`). |
| `metadata/<repo>/index/` | mirror copy (`src/lcsas/staging/metadata.py:50`) | Encrypted blob → pack offset map. |
| `metadata/<repo>/snapshots/` | mirror copy (`src/lcsas/staging/metadata.py:50`) | Encrypted snapshot pointers (root tree IDs). |
| `metadata/<repo>/keys/` | mirror copy (`src/lcsas/staging/metadata.py:50`) | scrypt-protected master key files. |
| `metadata/<repo>/config` | mirror copy (`src/lcsas/staging/metadata.py:57`) | Repo version + chunker params. |
| `catalog.db` | SQLite catalog (`src/lcsas/staging/metadata.py:61`) | Holographic archive catalog — every disc carries the full catalog. |
| `volume_info.json` | written by `write_volume_info` (`src/lcsas/staging/metadata.py:66`) | UUID, label, pack manifest. |
| `standalone_restorer.py` | written by `write_standalone_restorer` (`src/lcsas/staging/metadata.py:133`) | The pure-Python restorer (this document's hero). |
| `RESTORE_INSTRUCTIONS.txt` | written by `write_restore_instructions` (`src/lcsas/staging/metadata.py:148`) | Plain-text steps for a human. |
| `START_HERE.txt`, `KEY_INFO.txt`, `CONFIG_SUMMARY.txt`, `DISC_CARE.txt` | survivability docs (`src/lcsas/staging/metadata.py:253`, `:348`, `:396`, `:445`) | Onboarding for a non-technical finder. |
| `lcsas_src/restore/`, `lcsas_src/utils/`, `lcsas_src/db/` | `write_lcsas_source` (`src/lcsas/staging/metadata.py:106`) | The LCSAS source subpackages (for inspection or re-running). |

The encryption key (`KEY_INFO.txt` lists which one) is **never** on the
disc; the operator must supply it
(`src/lcsas/staging/metadata.py:172`).

---

## Workflow A — Mount a single data disc anywhere Python runs

**Purpose:** Make the disc's filesystem visible to a Python interpreter
on any OS so that `standalone_restorer.py` can run against it.

**Prerequisites:**

- Python 3.10+ (stdlib only).
- The data disc, either physical (BD/DVD/M-Disc) or as an ISO image.
- Read access to the disc (no write needed).
- The encryption key file (NOT on the disc — see
  `src/lcsas/staging/metadata.py:172`).

**Steps:**

1. Insert the optical disc, or copy the ISO to local storage.
2. Mount or extract the disc using whatever the host OS provides
   (`src/lcsas/staging/metadata.py:205`–`211`):
   - Linux: `sudo mount -o loop,ro VOLUME.iso /mnt/disc`
   - macOS: `hdiutil attach VOLUME.iso` (auto-mounts read-only).
   - Windows: right-click the `.iso` → *Mount*, or insert physical
     disc — Explorer assigns a drive letter.
   - Anywhere with `7z`: `7z x VOLUME.iso -o/tmp/vol1/`
     (`src/lcsas/staging/metadata.py:208`).
   - Anywhere with `xorriso`: `xorriso -indev VOLUME.iso -osirrox on
     -extract / /tmp/vol1/` (`src/lcsas/staging/metadata.py:211`).
3. Confirm the mount/extract is complete by listing the root and
   checking for `standalone_restorer.py`, `catalog.db`, and
   `metadata/` (`src/lcsas/staging/metadata.py:231`–`246`).
4. Note the mount path — call it `DISC` in later steps.

**Expected outcome:** `DISC/standalone_restorer.py`,
`DISC/catalog.db`, `DISC/data/`, and `DISC/metadata/<repo>/` are all
readable from the chosen host.

**Variant axes that apply:**

- OS: Linux, macOS, Windows, BSDs — anywhere CPython 3.10+ runs.
- Recovery tier: always 5.

**Test coverage:**

- Existing: `tests/integration/test_disc_only_restore.py:422` covers the
  `xorriso`-based ISO extraction path used by the wider test suite.
- Existing: `tests/integration/test_disc_only_restore.py:527` proves
  every ISO carries `volume_info.json` and a working `catalog.db`.
- Gap: no automated check that the manual `mount -o loop,ro` and `7z x`
  variants leave the same on-disc filenames intact (they should — these
  are filesystem-level extracts of the same ISO9660/Joliet image
  produced by `iso/xorriso.py`).

**Source refs:**
`src/lcsas/staging/metadata.py:148`–`246`,
`src/lcsas/staging/metadata.py:133`–`146`,
`tests/integration/test_disc_only_restore.py:422`–`468`.

---

## Workflow B — Run `standalone_restorer.py` from the disc

**Purpose:** Restore data using **only** the Python interpreter and the
disc — no LCSAS install, no rustic, no network.

**Prerequisites:**

- Disc mounted/extracted per Workflow A.
- Python ≥ 3.10
  (`src/lcsas/staging/metadata.py:199`,
  `src/lcsas/restore/standalone_builder.py:6`).
- Password file holding the repo key on its first line
  (`src/lcsas/restore/restic_fallback.py:333`).
- Optional: `pip install zstandard` if any pack is zstd-compressed
  (`src/lcsas/restore/restic_fallback.py:62`–`88`,
  `src/lcsas/restore/restic_fallback.py:200`).

**Steps:**

1. Build a "cache" directory the restorer can read by linking or
   copying the disc's `metadata/<repo>/` and `data/` into one tree.
   Layout expected by `PurePythonRestorer`:

   ```
   /tmp/cache/
     config
     keys/
     index/
     snapshots/
     data/<flat pack files>
   ```

   The restorer's `_find_pack_path` accepts either the standard
   two-level `data/<prefix>/<hash>` layout or LCSAS's flat
   `data/<hash>` (`src/lcsas/restore/restic_fallback.py:539`–`556`), so
   the on-disc tree can be linked verbatim. Linking (instead of
   copying) the on-disc `data/` keeps the cache footprint near zero
   (`src/lcsas/cli/main.py:2233`–`2246`).
2. Run the restorer from the disc using the host's `python3`
   (`src/lcsas/staging/metadata.py:195`–`200`):

   ```
   python3 DISC/standalone_restorer.py \
       --repo /tmp/cache \
       --password-file /path/to/keyfile \
       --target /path/to/output
   ```

   The script's CLI is generated by `_CLI_BLOCK`
   (`src/lcsas/restore/standalone_builder.py:128`–`205`).
3. To peek before restoring, list snapshots first
   (`src/lcsas/restore/standalone_builder.py:157`–`160`,
   `src/lcsas/restore/restic_fallback.py:377`):

   ```
   python3 DISC/standalone_restorer.py --repo /tmp/cache \
       --password-file /path/to/keyfile --list-snapshots
   ```

   Or print repo info (version, snapshot count, blob count, zstd
   availability)
   (`src/lcsas/restore/standalone_builder.py:161`–`164`,
   `src/lcsas/restore/restic_fallback.py:732`).
4. To pick a specific snapshot rather than the latest, pass
   `--snapshot <hex_id_or_prefix>`
   (`src/lcsas/restore/standalone_builder.py:153`–`156`,
   `src/lcsas/restore/restic_fallback.py:516`–`535`).
5. Each restored file's SHA-256 is verified against the blob ID
   recorded in the tree
   (`src/lcsas/restore/restic_fallback.py:587`–`593`).

**Expected outcome:** Files appear under `--target`, with permissions,
mtime/atime, xattrs and (where supported) hardlinks reconstructed best
effort (`src/lcsas/restore/restic_fallback.py:599`–`728`).

**Variant axes that apply:**

- OS: any Python-capable host (Linux/macOS/Windows/BSD).
- Recovery tier: 5.
- Compression: uncompressed restic v1, zstd v2 (auto-detected by magic
  bytes — `src/lcsas/restore/restic_fallback.py:433`–`440`,
  `src/lcsas/restore/restic_fallback.py:583`–`586`).
- Pack layout: flat (disc-style) or two-level (rustic-style)
  (`src/lcsas/restore/restic_fallback.py:539`–`556`).

**Test coverage:**

- Existing: `tests/unit/test_restic_fallback.py:427`–`510` —
  `PurePythonRestorer` smoke tests (verify_key, list_snapshots,
  repo_info, full restore, password-bytes path, target-dir creation,
  snapshot-by-prefix lookup) against a synthetic repo.
- Existing: `tests/unit/test_restic_fallback.py` permission, flat
  layout, symlink, hardlink, unsupported node, and xattr cases
  (`TestPermissionRestore`, `TestFlatLayout`, `TestSymlinkRestore`,
  `TestHardlinkRestore`, `TestUnsupportedNodeType`, `TestXattrRestore`).
- Existing: `tests/integration/test_pure_python_restore.py:521` (full
  family restore via `PurePythonRestorer`).
- Existing: `tests/integration/test_pure_python_restore.py:552` (full
  work restore including modified files).
- Existing: `tests/integration/test_pure_python_restore.py:582`
  byte-for-byte compares fallback output against `rustic restore`.
- Existing: `tests/integration/test_pure_python_restore.py:630` proves
  flat (LCSAS) layout works.
- Existing: `tests/integration/test_pure_python_restore.py:469`,
  `:481`, `:495` cover `verify_key`, wrong-password rejection, and
  snapshot listing against real rustic repos.
- Gap: no automated test runs the *generated*
  `standalone_restorer.py` as a subprocess (only the in-tree
  `PurePythonRestorer` class). Confidence comes indirectly from
  `src/lcsas/restore/standalone_builder.py` being a literal
  concatenation of the same modules.

**Source refs:**
`src/lcsas/restore/restic_fallback.py:303`–`595`,
`src/lcsas/restore/standalone_builder.py:30`–`205`,
`src/lcsas/staging/metadata.py:133`–`146`,
`tests/integration/test_pure_python_restore.py:463`–`667`.

---

## Workflow C — `lcsas restore from-disc` (convenience wrapper)

**Purpose:** When LCSAS *is* installed (e.g., from the meta-volume's
source bundle) but no working `rustic`/`restic` binary is present, the
`lcsas restore standalone` / `from-disc` subcommand orchestrates
Workflow B and auto-falls-back to `PurePythonRestorer`.

**Prerequisites:**

- LCSAS package importable on the host (`pip install -e .` from the
  bundled `lcsas_src/` or the meta-volume tree).
- A mounted disc (path `DISC`) containing `catalog.db` and
  `metadata/<repo>/`.
- The password file.
- Optional: `--volume-dir` of pre-extracted other discs for batch
  restore.

**Steps:**

1. Verify the disc path is a directory
   (`src/lcsas/cli/main.py:2118`).
2. Locate `catalog.db` — default `DISC/catalog.db`, override with
   `--catalog`
   (`src/lcsas/cli/main.py:2132`).
3. Copy `catalog.db` into a temp dir to avoid locking the read-only
   mount (`src/lcsas/cli/main.py:2153`–`2155`).
4. Read the disc-resident catalog to pick the repository (single repo
   = auto-selected; multi-repo = `--repo NAME`)
   (`src/lcsas/cli/main.py:2159`–`2191`).
5. Locate `metadata/<repo>/` on the disc
   (`src/lcsas/cli/main.py:2196`–`2204`).
6. Probe for a usable `rustic` binary; if absent, mark
   `rustic_available = False`
   (`src/lcsas/cli/main.py:2215`–`2219`).
7. `RestoreExecutor.prepare_cache` copies the disc metadata into the
   temp cache (`src/lcsas/cli/main.py:2224`–`2225`,
   `src/lcsas/restore/executor.py:75`).
8. If no rustic, symlink `DISC/data/` into `cache/data/` so the
   restorer can read packs without copying gigabytes
   (`src/lcsas/cli/main.py:2233`–`2253`). For multi-disc snapshots,
   merge other discs' `data/` into the cache *before* running.
9. Invoke `PurePythonRestorer.restore(target, snapshot_id)` —
   `--snapshot latest` is mapped to `None`
   (`src/lcsas/cli/main.py:2262`–`2271`).
10. Errors print a fallback hint pointing back to
    `standalone_restorer.py` if rustic was expected but absent
    (`src/lcsas/cli/main.py:2317`–`2326`,
    `:2533`–`:2540`).

**Expected outcome:** Files restored to `target_path`, log line
identifying the snapshot ID and hostname
(`src/lcsas/cli/main.py:2272`–`2276`).

**Variant axes that apply:**

- OS: Linux/macOS/Windows wherever LCSAS imports.
- Recovery tier: 5 (auto-degrades from 4 if rustic missing).
- Mode: interactive (single disc + prompts) or batch
  (`--volume-dir`)
  (`src/lcsas/cli/main.py:2384`,
  `:2430`–`:2438`). Note: batch and interactive multi-disc modes are
  rustic-paths; pure-Python mode in this wrapper returns at
  `:2277` after restoring whatever packs are reachable from the
  mounted/symlinked `data/`.
- Skip-verify: `--skip-verify` disables SHA-256 ingest verification
  (`src/lcsas/cli/main.py:2321`–`2323`).

**Test coverage:**

- Existing: argparse registration and option defaults —
  `tests/unit/test_restore_from_disc.py:83`–`131` (`TestFromDiscParser`).
- Existing: validation failures (missing disc path, missing catalog,
  no repos, missing metadata, no rustic, no TTY) —
  `tests/unit/test_restore_from_disc.py:133`–`257`
  (`TestFromDiscValidation`).
- Existing: batch mode + repo auto-select + custom catalog —
  `tests/unit/test_restore_from_disc.py:259`–`419`
  (`TestFromDiscBatchMode`).
- Existing: end-to-end behaviour of the wrapper components is
  exercised by `tests/integration/test_disc_only_restore.py` (rustic
  path) and `tests/integration/test_pure_python_restore.py` (fallback
  path).
- Gap: no test drives the pure-Python branch of `cmd_restore_from_disc`
  end-to-end through the CLI (the function returns at `:2277`
  separately from the rustic branch).

**Source refs:**
`src/lcsas/cli/main.py:277`–`323` (argparser),
`src/lcsas/cli/main.py:2089`–`2277` (handler — pure-Python branch),
`src/lcsas/cli/main.py:2721`–`2722` (dispatch),
`src/lcsas/restore/executor.py:75`–`122` (`prepare_cache`).

---

## Workflow D — Use the holographic SQLite catalog

**Purpose:** Inspect the on-disc archive catalog to discover what packs
exist, where they live (which physical volume), and which repos they
belong to — all without any central server.

**Prerequisites:**

- Disc mounted/extracted per Workflow A.
- Any SQLite client (`sqlite3` CLI, DB Browser, or Python `sqlite3`
  stdlib).

**Steps:**

1. Confirm the catalog file is present
   (`src/lcsas/staging/metadata.py:61`–`64`):

   ```
   ls DISC/catalog.db
   ```

2. Open it read-only:

   ```
   sqlite3 DISC/catalog.db
   ```

3. List repositories the archive knows about
   (`src/lcsas/cli/main.py:2159`–`2161`):

   ```
   SELECT repo_id, name, mirror_path FROM repositories;
   ```

4. List volumes (every disc burned in the archive — the catalog is
   cumulative, not just this disc) and check coverage:

   ```
   SELECT label, uuid, status FROM volumes ORDER BY label;
   ```

   The catalog on the **latest-burned** disc lists *every* prior
   volume — verified by
   `tests/integration/test_disc_only_restore.py:577`–`595`.
5. Find which volume holds a particular pack (used by
   `RestorePlanner.generate_pick_list_v2`):

   ```
   SELECT v.label
     FROM volume_packs vp
     JOIN volumes v  ON v.volume_id = vp.volume_id
     JOIN packs   p  ON p.pack_id   = vp.pack_id
    WHERE p.sha256 = '<sha>';
   ```

   Demonstrated end-to-end in
   `tests/integration/test_disc_only_restore.py:722`–`758`.
6. Read the human-friendly `volume_info.json` for the disc's own
   identity:

   ```
   cat DISC/volume_info.json
   ```

   Contains `uuid`, `label`, `media_type`, `pack_count`, `total_bytes`,
   `repositories`, `sha256_manifest`
   (`src/lcsas/staging/metadata.py:82`–`100`).

**Expected outcome:** A complete inventory of the entire archive
reconstructed from the single mounted disc, including which other
physical volumes you must fetch to complete a snapshot.

**Variant axes that apply:**

- OS: any SQLite-capable host.
- Recovery tier: 5 (also useful at tiers 3 and 4 for planning).

**Test coverage:**

- Existing: `tests/integration/test_disc_only_restore.py:527`–`549`
  (catalog tables present on every ISO).
- Existing: `tests/integration/test_disc_only_restore.py:577`–`595`
  (latest disc lists all volumes).
- Existing: `tests/integration/test_disc_only_restore.py:722`–`758`
  (pick list generated from on-disc catalog).
- Gap: no test exercises `volume_info.json` parsing — only existence
  (`:527`–`536`).

**Source refs:**
`src/lcsas/staging/metadata.py:61`–`104`,
`tests/integration/test_disc_only_restore.py:527`–`758`,
`CLAUDE.md` (*Holographic catalog*).

---

## Workflow E — AES-256-CTR + Poly1305 + zstd decrypt path

**Purpose:** Understand and (if needed) audit the crypto path the
pure-Python restorer takes. Every restic blob on the disc passes through
the same primitives.

**Prerequisites:**

- Read access to a pack file and the repo's metadata.
- Python 3.10+ stdlib (provides `hashlib.scrypt`).
- The repository password.

**Steps:**

1. **Master-key recovery (per repo, once).** Open a JSON file from
   `metadata/<repo>/keys/` and run scrypt with the params it embeds
   (`N`, `r`, `p`, default `N=32768, r=8, p=1`)
   (`src/lcsas/restore/restic_fallback.py:213`–`224`).
2. **Split the 64-byte derived key** into a 32-byte AES-256 key, a
   16-byte AES-128 key for Poly1305 nonce encryption, and a 16-byte
   Poly1305 `r` key
   (`src/lcsas/restore/restic_fallback.py:226`–`229`).
3. **Decrypt the key file's `data`** — format `IV(16) || ct || MAC(16)`
   — using authenticated decryption
   (`src/lcsas/restore/restic_fallback.py:145`–`184`). The MAC is
   computed as `s = AES-128-ECB(mac_k, IV); tag = Poly1305(mac_r, s,
   ct)` (`src/lcsas/restore/restic_fallback.py:99`–`126`,
   `:176`–`179`). Comparison is constant-time
   (`:187`–`194`).
4. **Pack blob read.** Look up the blob in the merged
   `metadata/<repo>/index/` files (`pack_id`, `offset`, `length`,
   `type`, `uncompressed_length`)
   (`src/lcsas/restore/restic_fallback.py:442`–`480`). Superseded
   index files are skipped (`:451`–`462`).
5. **Locate the pack** under `data/<prefix>/<id>` or `data/<id>`
   (`src/lcsas/restore/restic_fallback.py:539`–`556`), seek to
   `offset`, read `length` bytes.
6. **AES-256-CTR decrypt** the blob using the master key
   (`src/lcsas/restore/restic_fallback.py:574`–`578`). The pure-Python
   AES implementation is in `src/lcsas/restore/_aes_pure.py` and is
   exercised against NIST FIPS-197 / SP 800-38A vectors in
   `tests/unit/test_aes_pure.py:36`–`126`.
7. **zstd-decompress** if the decrypted blob starts with the magic
   `\x28\xB5\x2F\xFD` (repo v2 inline frame —
   `src/lcsas/restore/restic_fallback.py:583`–`586`). For standalone
   files (`index/`, `snapshots/`) a leading compression-type byte is
   stripped first (`:432`–`440`). Requires the optional `zstandard`
   pip package (`:62`–`88`).
8. **Verify** the decrypted blob's SHA-256 equals the blob ID
   (`src/lcsas/restore/restic_fallback.py:587`–`593`).

**Expected outcome:** Plaintext file content or tree JSON, with
integrity proven by MAC + SHA-256.

**Variant axes that apply:**

- Repo format: v1 (no compression-type prefix) and v2 (zstd-capable)
  (`src/lcsas/restore/restic_fallback.py:421`–`440`).
- Compression: optional `zstandard` package
  (`src/lcsas/restore/restic_fallback.py:79`–`88`).
- Crypto primitives: all pure-Python, no `cryptography` / OpenSSL
  required.

**Test coverage:**

- Existing: `tests/unit/test_aes_pure.py:12`–`126` — key schedule,
  AES-128/256 ECB NIST vectors, CTR round-trip, NIST CTR vector,
  empty, partial block, multi-block.
- Existing: `tests/unit/test_restic_fallback.py:268`–`305` —
  Poly1305 RFC 8439 vector, empty message, `_clamp_r` bit clearing.
- Existing: `tests/unit/test_restic_fallback.py:307`–`361` —
  authenticated encryption round-trip, wrong-key rejection,
  tampered-data rejection, too-short-data rejection, large-data
  round-trip.
- Existing: `tests/unit/test_restic_fallback.py:364`–`373` —
  constant-time equality.
- Existing: `tests/unit/test_restic_fallback.py:377`–`391` —
  timestamp parsing (nanosecond, microsecond, no-fractional).
- Existing: `tests/unit/test_restic_fallback.py:394`–`426` —
  scrypt key derivation, wrong-password rejection (synthetic key).
- Existing: `tests/integration/test_pure_python_restore.py:469`–`667`
  exercises the full crypto chain against real rustic-produced data
  (scrypt → master key → AES-CTR → Poly1305 → optional zstd → SHA-256).

**Source refs:**
`src/lcsas/restore/restic_fallback.py:13`–`24` (crypto stack table),
`src/lcsas/restore/restic_fallback.py:93`–`242` (KDF + AE),
`src/lcsas/restore/restic_fallback.py:411`–`507` (file/index/snapshot
decryption), `src/lcsas/restore/restic_fallback.py:558`–`595` (blob
read + verify), `src/lcsas/restore/_aes_pure.py`,
`tests/unit/test_aes_pure.py`.

---

## Workflow F — Recover a single file vs a full snapshot

**Purpose:** Match the recovery scope to the urgency. Tier 5 supports
both, but the pure-Python tree traversal is recursive — there is no
built-in "extract one path" flag.

**Prerequisites:** Workflow B prerequisites.

### F.1 — Full snapshot restore (the default)

**Steps:**

1. Build the cache directory (Workflow B step 1).
2. Run the restorer with `--target /path/to/output` and either no
   `--snapshot` (latest) or a hex prefix
   (`src/lcsas/restore/restic_fallback.py:344`–`375`).
3. The restorer recursively walks the snapshot's root tree
   (`src/lcsas/restore/restic_fallback.py:372`,
   `src/lcsas/restore/restic_fallback.py:599`–`682`), reconstructing
   directories, files, symlinks (validated against path traversal —
   `:610`–`618`, `:655`–`671`), and hardlinks
   (`:622`–`644`). Metadata (mode, mtime, atime, xattrs) is restored
   best effort (`:696`–`728`).

**Expected outcome:** A complete tree under `--target` matching the
snapshot, byte-for-byte identical to a `rustic restore` of the same
snapshot — proven by
`tests/integration/test_pure_python_restore.py:582`.

### F.2 — Single-file recovery

There is **no** `--include` flag on the standalone restorer
(`src/lcsas/restore/standalone_builder.py:128`–`205`). Two practical
options:

1. **Restore the whole snapshot to scratch space**, then copy the one
   file out. Acceptable when the snapshot is small relative to free
   space. Use `--list-snapshots`
   (`src/lcsas/restore/standalone_builder.py:157`–`160`) first to pick
   the right one.
2. **Drive `PurePythonRestorer` from a Python REPL or short script**:
   import the class from the on-disc `standalone_restorer.py`
   (or from `lcsas_src/restore/restic_fallback.py` on the disc —
   `src/lcsas/staging/metadata.py:119`), load the master key, walk
   `tree → subtree → ... → node`, and read only the desired file's
   content blobs via the private `_read_blob` API
   (`src/lcsas/restore/restic_fallback.py:558`–`595`). Each file's
   content is the concatenation of its `content` blob IDs
   (`src/lcsas/restore/restic_fallback.py:687`–`692`).

**Expected outcome:** Targeted file extracted with the same integrity
guarantees as a full restore (SHA-256 verified per blob).

**Variant axes that apply:**

- OS: any Python-capable host.
- Recovery tier: 5.
- Scope: full snapshot (supported directly) or single-file (manual
  via Python API).

**Test coverage:**

- Existing: full-restore correctness —
  `tests/integration/test_pure_python_restore.py:521`, `:552`, `:582`,
  `:630`, `:669`.
- Existing: snapshot listing —
  `tests/integration/test_pure_python_restore.py:495`.
- Existing: snapshot lookup by ID prefix —
  `src/lcsas/restore/restic_fallback.py:516`–`535` (covered indirectly
  by the restore tests).
- Gap: no test or CLI flag for a single-file extraction. This is
  intentional — Tier 5 is "get everything back, slowly".

**Source refs:**
`src/lcsas/restore/restic_fallback.py:344`–`728`,
`src/lcsas/restore/standalone_builder.py:128`–`205`,
`tests/integration/test_pure_python_restore.py:495`–`687`.

---

## Hard limits of Tier 5

This path deliberately trades capability for portability. It **cannot**:

- **Stitch packs across multiple discs.** `PurePythonRestorer` reads
  packs from a single repository directory
  (`src/lcsas/restore/restic_fallback.py:303`–`341`). The `lcsas
  restore from-disc` wrapper compensates by symlinking the disc's
  `data/` into the cache and instructing the operator to merge other
  discs manually (`src/lcsas/cli/main.py:2255`–`2261`). The standalone
  script offers no such orchestration.
- **Locate missing packs.** If a required pack is on a disc you have
  not mounted, the SHA-256 will not be found in `_find_pack_path` and
  the restore aborts (`src/lcsas/restore/restic_fallback.py:553`).
  Consult `catalog.db` (Workflow D) to discover where the missing pack
  lives and mount that disc.
- **Deduplicate or prune.** It is a *reader*, not a repo manager — no
  `forget`, `prune`, or `repair-index` equivalents exist in
  `restic_fallback.py`.
- **Re-encrypt or rotate keys.** No write path through the master key
  exists.
- **Verify ECC.** Read errors on a degraded disc must be repaired with
  `dvdisaster` *before* Tier 5 starts, because the restorer reads
  pack bytes verbatim and verifies SHA-256 afterwards — a flipped bit
  surfaces as `IntegrityError` (`src/lcsas/restore/restic_fallback.py:587`).
- **Run fast.** Expect ~1 MB/s on modern hardware
  (`src/lcsas/restore/restic_fallback.py:10`).
- **Handle a repository where the password is wrong.** Wrong password
  raises `IntegrityError` from MAC verification
  (`src/lcsas/restore/restic_fallback.py:166`–`183`,
  `:272`); use `--info`/`verify_key()` to test
  (`src/lcsas/restore/restic_fallback.py:383`–`392`).
- **Skip a damaged blob and continue.** A SHA-256 mismatch aborts the
  current restore (`src/lcsas/restore/restic_fallback.py:587`–`593`).

If you need cross-disc reconstruction or pack repair, escalate to Tier
3/4 (`docs/workflows/restore-host-linux.md`,
`docs/workflows/meta-volume.md`) — both still leverage the same
holographic catalog this tier relies on.

---

## Test coverage matrix

| Concern | Test | Status |
| --- | --- | --- |
| AES-128/256 key schedule (FIPS 197) | `tests/unit/test_aes_pure.py:12`–`30` | Covered |
| AES-128 ECB NIST vector | `tests/unit/test_aes_pure.py:36` | Covered |
| AES-256 ECB NIST vector | `tests/unit/test_aes_pure.py:46` | Covered |
| AES-CTR round-trip + NIST | `tests/unit/test_aes_pure.py:74`–`126` | Covered |
| Poly1305 RFC 8439 vector | `tests/unit/test_restic_fallback.py:268`–`291` | Covered |
| `_clamp_r` bit clearing | `tests/unit/test_restic_fallback.py:292`–`305` | Covered |
| Authenticated encryption round-trip | `tests/unit/test_restic_fallback.py:309`–`361` | Covered |
| Constant-time equality | `tests/unit/test_restic_fallback.py:364`–`373` | Covered |
| Timestamp parsing | `tests/unit/test_restic_fallback.py:377`–`391` | Covered |
| scrypt master-key derivation (synthetic) | `tests/unit/test_restic_fallback.py:394`–`426` | Covered |
| `PurePythonRestorer` core methods (synthetic) | `tests/unit/test_restic_fallback.py:427`–`510` | Covered |
| Permission / flat-layout / symlink / hardlink / xattr restore | `tests/unit/test_restic_fallback.py` (later classes) | Covered |
| scrypt → master key (real rustic) | `tests/integration/test_pure_python_restore.py:469` | Covered |
| Wrong-password rejection (real rustic) | `tests/integration/test_pure_python_restore.py:481` | Covered |
| Snapshot listing | `tests/integration/test_pure_python_restore.py:495` | Covered |
| Repo info (version, blob count) | `tests/integration/test_pure_python_restore.py:508` | Covered |
| Full restore (family — initial + incremental) | `tests/integration/test_pure_python_restore.py:521` | Covered |
| Full restore (work — modified files) | `tests/integration/test_pure_python_restore.py:552` | Covered |
| Pure-Python ≡ rustic byte-for-byte | `tests/integration/test_pure_python_restore.py:582` | Covered |
| Flat (LCSAS disc) pack layout | `tests/integration/test_pure_python_restore.py:630` | Covered |
| Incremental file presence | `tests/integration/test_pure_python_restore.py:669` | Covered |
| `RestoreExecutor.prepare_cache` + fallback | `tests/integration/test_pure_python_restore.py:688` | Covered |
| Every ISO is self-describing (catalog + volume_info) | `tests/integration/test_disc_only_restore.py:527` | Covered |
| Holographic metadata on every ISO | `tests/integration/test_disc_only_restore.py:551` | Covered |
| Latest catalog knows all volumes | `tests/integration/test_disc_only_restore.py:577` | Covered |
| Pick list from on-disc catalog | `tests/integration/test_disc_only_restore.py:722` | Covered |
| Packs span multiple discs | `tests/integration/test_disc_only_restore.py:760` | Covered |
| `cmd_restore_from_disc` argparser registration | `tests/unit/test_restore_from_disc.py:83`–`131` | Covered |
| `cmd_restore_from_disc` validation paths | `tests/unit/test_restore_from_disc.py:133`–`257` | Covered |
| `cmd_restore_from_disc` batch mode | `tests/unit/test_restore_from_disc.py:259`–`419` | Covered |
| `cmd_restore_from_disc` pure-Python branch E2E | — | **Gap** |
| Generated `standalone_restorer.py` run as subprocess | — | **Gap** |
| `volume_info.json` shape | — | **Gap** (existence only) |
| Single-file extraction API | — | **Gap** (by design — not exposed) |

---

## Consolidated source refs

Required reading (repo-relative):

- `CLAUDE.md`
- `src/lcsas/cli/main.py` — argparser `:277`–`323`,
  handler `cmd_restore_from_disc` `:2089`–`2548`, dispatch
  `:2716`–`:2722`.
- `src/lcsas/restore/restic_fallback.py` — pure-Python AES/zstd reader
  (full file).
- `src/lcsas/restore/_aes_pure.py` — AES primitives.
- `src/lcsas/restore/standalone_builder.py` — builds
  `standalone_restorer.py` (`:30`–`:205`).
- `src/lcsas/staging/metadata.py` — `HolographicInjector`
  (`:28`–`:535`), `write_standalone_restorer` (`:133`–`:146`),
  `write_lcsas_source` (`:106`–`:131`), `write_restore_instructions`
  (`:148`–`:251`), `inject_catalog` (`:61`–`:64`), `write_volume_info`
  (`:66`–`:104`).
- `src/lcsas/restore/executor.py` — `prepare_cache` (`:75`),
  `ingest_volume` (`:122`), `verify_cache_completeness` (`:256`).
- `tests/unit/test_aes_pure.py` — AES-CTR NIST vectors.
- `tests/integration/test_disc_only_restore.py` — end-to-end
  rustic-path multi-disc proof.
- `tests/integration/test_pure_python_restore.py` — end-to-end
  fallback-path proof.
