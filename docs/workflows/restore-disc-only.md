# Disc-Only Pure-Python Restore (Tier 3)

> Recovery tier 3 — the fallback of last resort. A single LCSAS data disc,
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

1. [When to use Tier 3](#when-to-use-tier-3)
2. [What every data disc contains](#what-every-data-disc-contains)
3. [Workflow A — Mount a single data disc anywhere Python runs](#workflow-a--mount-a-single-data-disc-anywhere-python-runs)
4. [Workflow B — Run `standalone_restorer.py` from the disc](#workflow-b--run-standalone_restorerpy-from-the-disc)
5. [Workflow C — `lcsas restore standalone` (convenience wrapper)](#workflow-c--lcsas-restore-standalone-convenience-wrapper)
6. [Workflow D — Use the holographic SQLite catalog](#workflow-d--use-the-holographic-sqlite-catalog)
7. [Workflow E — AES-256-CTR + Poly1305 + zstd decrypt path](#workflow-e--aes-256-ctr--poly1305--zstd-decrypt-path)
8. [Workflow F — Recover a single file vs a full snapshot](#workflow-f--recover-a-single-file-vs-a-full-snapshot)
9. [Hard limits of Tier 3](#hard-limits-of-tier-3)
10. [Test coverage matrix](#test-coverage-matrix)
11. [Consolidated source refs](#consolidated-source-refs)

---

## When to use Tier 3

Use this path **only** if all higher tiers are unreachable. Tier 3
operates at ~1 MB/s (module docstring,
`src/lcsas/restore/restic_fallback.py`) and reads packs only from its
configured search paths — multi-disc snapshots need operator help: merge
every disc's `data/` into one cache first, pass one `--mount-point` per
disc, or answer the interactive disc-swap prompt
(`PurePythonRestorer._find_pack_path()` in
`src/lcsas/restore/restic_fallback.py`).

Pick Tier 3 when, and only when:

- No working `rustic` or `restic` binary is available for the host
  architecture (the rustic probe in `cmd_restore_from_disc()`,
  `src/lcsas/cli/main.py`).
- The meta-volume is lost, damaged, or its bundled binaries cannot
  execute (different CPU, glibc ABI break).
- Internet access is unavailable, so a new binary cannot be downloaded.
- You hold **at least one** data disc (and the encryption key) whose
  contents include the snapshot you want.

If the snapshot you want spans multiple discs and rustic is unavailable,
either extract pack files from every relevant disc into a single cache
directory **before** invoking the restorer (the merge hint logged by
`cmd_restore_from_disc()` in `src/lcsas/cli/main.py`), or lean on the
restorer's own disc handling: since #234 `standalone_restorer.py`
accepts repeatable `--mount-point` roots and, when interactive, prompts
for the next disc whenever a pack is not found in any search path
(`PurePythonRestorer._find_pack_path()`).

---

## What every data disc contains

The `HolographicInjector` ensures every disc is self-describing
(`src/lcsas/staging/metadata.py`). After
`HolographicInjector.write_*` runs during the burn pipeline, the root
of each ISO contains:

| Path on disc | Source | Purpose for Tier 3 |
| --- | --- | --- |
| `data/` | hardlinked packs (`staging/builder.py`) | The encrypted restic pack files. Two-level `data/<prefix>/<sha256>` layout (`pack_dest_path()` in `src/lcsas/utils/pack_layout.py`); the restorer also accepts flat `data/<sha256>` (`PurePythonRestorer._find_pack_path()`). |
| `metadata/<repo>/index/` | mirror copy (`HolographicInjector.inject_metadata()`) | Encrypted blob → pack offset map. |
| `metadata/<repo>/snapshots/` | mirror copy (`inject_metadata()`) | Encrypted snapshot pointers (root tree IDs). |
| `metadata/<repo>/keys/` | mirror copy (`inject_metadata()`) | scrypt-protected master key files. |
| `metadata/<repo>/config` | mirror copy (`inject_metadata()`) | Repo version + chunker params. |
| `catalog.db` | SQLite catalog (`inject_catalog()`) | Holographic archive catalog — every disc carries the full catalog. |
| `volume_info.json` | written by `write_volume_info()` | UUID, label, pack manifest. |
| `standalone_restorer.py` | written by `write_standalone_restorer()` | The pure-Python restorer (this document's hero). |
| `RESTORE_INSTRUCTIONS.txt` | written by `write_restore_instructions()` | Plain-text steps for a human. |
| `START_HERE.txt`, `KEY_INFO.txt`, `CONFIG_SUMMARY.txt`, `DISC_CARE.txt` | survivability docs (`write_start_here()`, `write_key_info()`, `write_config_summary()`, `write_disc_care()`) | Onboarding for a non-technical finder. |
| `lcsas_src/restore/`, `lcsas_src/utils/`, `lcsas_src/db/` | `write_lcsas_source()` | The LCSAS source subpackages (for inspection or re-running). |

The encryption key (`KEY_INFO.txt` lists which one) is **never** on the
disc; the operator must supply it
(stated in the text written by
`HolographicInjector.write_restore_instructions()`).

---

## Workflow A — Mount a single data disc anywhere Python runs

**Purpose:** Make the disc's filesystem visible to a Python interpreter
on any OS so that `standalone_restorer.py` can run against it.

**Prerequisites:**

- Python 3.10+ (stdlib only).
- The data disc, either physical (BD/DVD/M-Disc) or as an ISO image.
- Read access to the disc (no write needed).
- The encryption key file (NOT on the disc — see the note written by
  `HolographicInjector.write_restore_instructions()`,
  `src/lcsas/staging/metadata.py`).

**Steps:**

1. Insert the optical disc, or copy the ISO to local storage.
2. Mount or extract the disc using whatever the host OS provides
   (the "HOW TO RESTORE (manual)" section of the on-disc
   `RESTORE_INSTRUCTIONS.txt`, written by
   `write_restore_instructions()`):
   - Linux: `sudo mount -o loop,ro VOLUME.iso /mnt/disc`
   - macOS: `hdiutil attach VOLUME.iso` (auto-mounts read-only).
   - Windows: right-click the `.iso` → *Mount*, or insert physical
     disc — Explorer assigns a drive letter.
   - Anywhere with `7z`: `7z x VOLUME.iso -o/tmp/vol1/`
     (option B in the same instructions text).
   - Anywhere with `xorriso`: `xorriso -indev VOLUME.iso -osirrox on
     -extract / /tmp/vol1/` (option C in the same instructions text).
3. Confirm the mount/extract is complete by listing the root and
   checking for `standalone_restorer.py`, `catalog.db`, and
   `metadata/` (the "DISC CONTENTS" listing in
   `RESTORE_INSTRUCTIONS.txt`).
4. Note the mount path — call it `DISC` in later steps.

**Expected outcome:** `DISC/standalone_restorer.py`,
`DISC/catalog.db`, `DISC/data/`, and `DISC/metadata/<repo>/` are all
readable from the chosen host.

**Variant axes that apply:**

- OS: Linux, macOS, Windows, BSDs — anywhere CPython 3.10+ runs.
- Recovery tier: always 3.

**Test coverage:**

- Existing: `tests/integration/test_disc_only_restore.py::TestDiscOnlyRestore._extract_iso`
  covers the `xorriso`-based ISO extraction path used by the wider test
  suite.
- Existing: `tests/integration/test_disc_only_restore.py::TestDiscOnlyRestore::test_isos_are_self_describing`
  proves every ISO carries `volume_info.json` and a working `catalog.db`.
- Gap: no automated check that the manual `mount -o loop,ro` and `7z x`
  variants leave the same on-disc filenames intact (they should — these
  are filesystem-level extracts of the same ISO9660/Joliet image
  produced by `iso/xorriso.py`).

**Source refs:**
`HolographicInjector.write_restore_instructions()` and
`write_standalone_restorer()` in `src/lcsas/staging/metadata.py`,
`TestDiscOnlyRestore._extract_iso` in
`tests/integration/test_disc_only_restore.py`.

---

## Workflow B — Run `standalone_restorer.py` from the disc

**Purpose:** Restore data using **only** the Python interpreter and the
disc — no LCSAS install, no rustic, no network.

**Prerequisites:**

- Disc mounted/extracted per Workflow A.
- Python ≥ 3.10
  (enforced by the generated script's version guard — `_HEADER` in
  `src/lcsas/restore/standalone_builder.py`).
- Password file holding the repo key on its first line
  (`PurePythonRestorer.__init__()` in
  `src/lcsas/restore/restic_fallback.py`).
- Optional: `pip install zstandard` for ~100x faster zstd
  decompression. Since RST-04 a pure-Python zstd decoder
  (`src/lcsas/restore/_zstd_pure.py`) is bundled into the generated
  script, so zstd-compressed repos restore with stdlib only
  (module-level `_decompress_zstd()` in
  `src/lcsas/restore/restic_fallback.py`).

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
     data/<pack files>
   ```

   The restorer's `_find_pack_path()` accepts both the standard
   two-level `data/<prefix>/<hash>` layout and a flat `data/<hash>`
   (`PurePythonRestorer._find_pack_path()` in
   `src/lcsas/restore/restic_fallback.py`), so
   the on-disc tree can be linked verbatim. Linking (instead of
   copying) the on-disc `data/` keeps the cache footprint near zero
   (the data-symlink step in `cmd_restore_from_disc()`,
   `src/lcsas/cli/main.py`).
2. Run the restorer from the disc using the host's `python3`
   (the "pure Python" section of the on-disc
   `RESTORE_INSTRUCTIONS.txt`, written by
   `write_restore_instructions()`):

   ```
   python3 DISC/standalone_restorer.py \
       --repo /tmp/cache \
       --password-file /path/to/keyfile \
       --target /path/to/output
   ```

   The script's CLI is generated by `_CLI_BLOCK`
   (`src/lcsas/restore/standalone_builder.py`).
3. To peek before restoring, list snapshots first
   (`--list-snapshots` in `_CLI_BLOCK`;
   `PurePythonRestorer.list_snapshots()`):

   ```
   python3 DISC/standalone_restorer.py --repo /tmp/cache \
       --password-file /path/to/keyfile --target /tmp/out \
       --list-snapshots
   ```

   (`--target` is a required flag even in list mode, but nothing is
   written to it — the CLI exits after printing the snapshot list.)

   Or print repo info (version, snapshot count, blob count, zstd
   availability)
   (`--info` in `_CLI_BLOCK`; `PurePythonRestorer.repo_info()`).
4. To pick a specific snapshot rather than the latest, pass
   `--snapshot <hex_id_or_prefix>`
   (`--snapshot` in `_CLI_BLOCK`;
   `PurePythonRestorer._find_snapshot()`).
5. Each restored file's SHA-256 is verified against the blob ID
   recorded in the tree
   (`PurePythonRestorer._read_blob()`).

**Expected outcome:** Files appear under `--target`, with permissions,
mtime/atime, xattrs and (where supported) hardlinks reconstructed best
effort (`PurePythonRestorer._restore_tree()` / `_apply_metadata()`).

**Variant axes that apply:**

- OS: any Python-capable host (Linux/macOS/Windows/BSD).
- Recovery tier: 3.
- Compression: uncompressed restic v1, zstd v2. Compression is
  discriminated by the index's `uncompressed_length` field, with a
  magic-bytes fallback for v1/legacy indexes
  (`PurePythonRestorer._read_blob()`); standalone repo files use a
  compression-type prefix byte (`_decrypt_file()`).
- Pack layout: two-level (discs and rustic mirrors alike) or flat
  (legacy) (`PurePythonRestorer._find_pack_path()`).

**Test coverage:**

- Existing: `tests/unit/test_restic_fallback.py::TestPurePythonRestorer` —
  `PurePythonRestorer` smoke tests (verify_key, list_snapshots,
  repo_info, full restore, password-bytes path, target-dir creation,
  snapshot-by-prefix lookup) against a synthetic repo.
- Existing: `tests/unit/test_restic_fallback.py` permission, flat
  layout, symlink, hardlink, unsupported node, and xattr cases
  (`TestPermissionRestore`, `TestFlatLayout`, `TestSymlinkRestore`,
  `TestHardlinkRestore`, `TestUnsupportedNodeType`, `TestXattrRestore`).
- Existing: `tests/integration/test_pure_python_restore.py::TestPurePythonFallbackRestore::test_fallback_restore_family`
  (full family restore via `PurePythonRestorer`).
- Existing: `...::test_fallback_restore_work` (full work restore
  including modified files).
- Existing: `...::test_fallback_matches_rustic_restore`
  byte-for-byte compares fallback output against `rustic restore`.
- Existing: `...::test_fallback_restore_with_flat_layout` proves
  flat (legacy) layout works.
- Existing: `...::test_fallback_verifies_key`,
  `test_fallback_rejects_wrong_password`, and
  `test_fallback_lists_snapshots` cover `verify_key`,
  wrong-password rejection, and snapshot listing against real rustic
  repos.
- Gap: no automated test runs the *generated*
  `standalone_restorer.py` as a subprocess (only the in-tree
  `PurePythonRestorer` class). Confidence comes indirectly from
  `src/lcsas/restore/standalone_builder.py` being a literal
  concatenation of the same modules.

**Source refs:**
`PurePythonRestorer` in `src/lcsas/restore/restic_fallback.py`,
`build_standalone()` + `_CLI_BLOCK` in
`src/lcsas/restore/standalone_builder.py`,
`HolographicInjector.write_standalone_restorer()` in
`src/lcsas/staging/metadata.py`,
`TestPurePythonFallbackRestore` in
`tests/integration/test_pure_python_restore.py`.

---

## Workflow C — `lcsas restore standalone` (convenience wrapper)

**Purpose:** When LCSAS *is* installed (e.g., from the meta-volume's
source bundle) but no working `rustic`/`restic` binary is present, the
`lcsas restore standalone` subcommand orchestrates Workflow B and
auto-falls-back to `PurePythonRestorer`.

**Prerequisites:**

- LCSAS package importable on the host (`pip install -e .` from the
  bundled `lcsas_src/` or the meta-volume tree).
- A mounted disc (path `DISC`) containing `catalog.db` and
  `metadata/<repo>/`.
- The password file.
- Optional: `--volume-dir` of pre-extracted other discs for batch
  restore.

All step references below are to `cmd_restore_from_disc()` in
`src/lcsas/cli/main.py` unless noted otherwise.

**Steps:**

1. Verify the disc path is a directory
   (early validation in `cmd_restore_from_disc()`).
2. Locate `catalog.db` — default `DISC/catalog.db`, override with
   `--catalog`.
3. Copy `catalog.db` into a temp dir to avoid locking the read-only
   mount.
4. Read the disc-resident catalog to pick the repository (single repo
   = auto-selected; multi-repo = `--repo NAME`).
5. Locate `metadata/<repo>/` on the disc.
6. Probe for a usable `rustic` binary; if absent, mark
   `rustic_available = False`
   (the `check_binary_version("rustic", ...)` probe).
7. `RestoreExecutor.prepare_cache()` copies the disc metadata into the
   temp cache (`src/lcsas/restore/executor.py`).
8. If no rustic, symlink `DISC/data/` into `cache/data/` so the
   restorer can read packs without copying gigabytes
   (the data-symlink block). For multi-disc snapshots,
   merge other discs' `data/` into the cache *before* running.
9. Invoke `PurePythonRestorer.restore(target, snapshot_id)` —
   `--snapshot latest` is mapped to `None`.
10. Errors print a fallback hint pointing back to
    `standalone_restorer.py` if rustic was expected but absent
    (the `FileNotFoundError` handlers around the dry-run and
    `execute_restore()` calls).

**Expected outcome:** Files restored to `target_path`, log line
identifying the snapshot ID and hostname
(the pure-Python success log in `cmd_restore_from_disc()`).
A partial pure-Python restore — some files skipped under tolerant
traversal — exits `2` and leaves a `RESTORE_FAILURES.txt` manifest
under the target.

**Variant axes that apply:**

- OS: Linux/macOS/Windows wherever LCSAS imports.
- Recovery tier: 3 (auto-degrades from 2 if rustic missing).
- Mode: interactive (single disc + prompts) or batch
  (`--volume-dir`)
  (the two ingest branches of `cmd_restore_from_disc()`). Note: batch
  and interactive multi-disc modes are rustic-paths; the pure-Python
  branch of `cmd_restore_from_disc()` returns before the rustic-path
  code, after restoring whatever packs are reachable from the
  mounted/symlinked `data/`.
- Skip-verify: `--skip-verify` disables SHA-256 ingest verification
  (passed through to `ingest_volume()` by
  `cmd_restore_from_disc()`).

**Test coverage:**

- Existing: argparse registration and option defaults —
  `tests/unit/test_restore_from_disc.py::TestFromDiscParser`.
- Existing: validation failures (missing disc path, missing catalog,
  no repos, missing metadata, no rustic, no TTY) —
  `tests/unit/test_restore_from_disc.py::TestFromDiscValidation`.
- Existing: batch mode + repo auto-select + custom catalog —
  `tests/unit/test_restore_from_disc.py::TestFromDiscBatchMode`.
- Existing: end-to-end behaviour of the wrapper components is
  exercised by `tests/integration/test_disc_only_restore.py` (rustic
  path) and `tests/integration/test_pure_python_restore.py` (fallback
  path).
- Gap: no test drives the pure-Python branch of `cmd_restore_from_disc`
  end-to-end through the CLI (that branch returns separately, before
  the rustic-path code).

**Source refs:**
the `restore standalone` subparser in `build_parser()`
(`src/lcsas/cli/main.py`),
`cmd_restore_from_disc()` in `src/lcsas/cli/main.py`
(pure-Python branch),
`dispatch()` in `src/lcsas/cli/main.py`,
`RestoreExecutor.prepare_cache()` in `src/lcsas/restore/executor.py`.

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
   (written by `HolographicInjector.inject_catalog()`,
   `src/lcsas/staging/metadata.py`):

   ```
   ls DISC/catalog.db
   ```

2. Open it read-only:

   ```
   sqlite3 DISC/catalog.db
   ```

3. List repositories the archive knows about
   (the same query `cmd_restore_from_disc()` runs against the
   disc catalog):

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
   `tests/integration/test_disc_only_restore.py::TestDiscOnlyRestore::test_latest_catalog_knows_all_volumes`.
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
   `tests/integration/test_disc_only_restore.py::TestDiscOnlyRestore::test_on_disc_catalog_enables_pick_list`.
6. Read the human-friendly `volume_info.json` for the disc's own
   identity:

   ```
   cat DISC/volume_info.json
   ```

   Contains `uuid`, `label`, `media_type`, `pack_count`, `total_bytes`,
   `repositories`, `sha256_manifest`
   (`HolographicInjector.write_volume_info()`).

**Expected outcome:** A complete inventory of the entire archive
reconstructed from the single mounted disc, including which other
physical volumes you must fetch to complete a snapshot.

**Variant axes that apply:**

- OS: any SQLite-capable host.
- Recovery tier: 3 (also useful at tiers 1 and 2 for planning).

**Test coverage:**

- Existing: `tests/integration/test_disc_only_restore.py::TestDiscOnlyRestore::test_isos_are_self_describing`
  (catalog tables present on every ISO).
- Existing: `...::test_latest_catalog_knows_all_volumes`
  (latest disc lists all volumes).
- Existing: `...::test_on_disc_catalog_enables_pick_list`
  (pick list generated from on-disc catalog).
- Gap: no test exercises `volume_info.json` parsing — only existence
  (asserted in `test_isos_are_self_describing`).

**Source refs:**
`HolographicInjector.inject_catalog()` and `write_volume_info()` in
`src/lcsas/staging/metadata.py`,
the `TestDiscOnlyRestore` catalog tests in
`tests/integration/test_disc_only_restore.py`,
`CLAUDE.md` (*Holographic catalog*).

---

## Workflow E — AES-256-CTR + Poly1305 + zstd decrypt path

**Purpose:** Understand and (if needed) audit the crypto path the
pure-Python restorer takes. Every restic blob on the disc passes through
the same primitives.

All symbol references below are to
`src/lcsas/restore/restic_fallback.py` unless noted otherwise.

**Prerequisites:**

- Read access to a pack file and the repo's metadata.
- Python 3.10+ stdlib (provides `hashlib.scrypt`).
- The repository password.

**Steps:**

1. **Master-key recovery (per repo, once).** Open a JSON file from
   `metadata/<repo>/keys/` and run scrypt with the params it embeds
   (`N`, `r`, `p`, default `N=32768, r=8, p=1`)
   (`_load_master_key()`).
2. **Split the 64-byte derived key** into a 32-byte AES-256 key, a
   16-byte AES-128 key for Poly1305 nonce encryption, and a 16-byte
   Poly1305 `r` key
   (`_load_master_key()`).
3. **Decrypt the key file's `data`** — format `IV(16) || ct || MAC(16)`
   — using authenticated decryption
   (`_decrypt_authenticated()`). The MAC is
   computed as `s = AES-128-ECB(mac_k, IV); tag = Poly1305(mac_r, s,
   ct)` (`_poly1305_mac()`, applied inside
   `_decrypt_authenticated()`). Comparison is constant-time
   (`_constant_time_eq()`).
4. **Pack blob read.** Look up the blob in the merged
   `metadata/<repo>/index/` files (`pack_id`, `offset`, `length`,
   `type`, `uncompressed_length`)
   (`_load_index()`). Superseded
   index files are skipped (first pass of `_load_index()`).
5. **Locate the pack** under `data/<prefix>/<id>` or `data/<id>`
   (`PurePythonRestorer._find_pack_path()`), seek to
   `offset`, read `length` bytes.
6. **AES-256-CTR decrypt** the blob using the master key
   (`_read_blob()` → `_decrypt()`). The pure-Python
   AES implementation is in `src/lcsas/restore/_aes_pure.py` and is
   exercised against NIST FIPS-197 / SP 800-38A vectors in
   `tests/unit/test_aes_pure.py` (`TestAESEncryptBlock`, `TestAESCTR`).
7. **zstd-decompress** when the index entry carries
   `uncompressed_length` (repo v2), with a magic-bytes
   (`\x28\xB5\x2F\xFD`) fallback for legacy/v1 indexes
   (`_read_blob()`). For standalone
   files (`index/`, `snapshots/`) a leading compression-type byte is
   stripped first (`_decrypt_file()`). Decompression uses the
   `zstandard` package when installed, else the bundled pure-Python
   decoder (`_decompress_zstd()`; `src/lcsas/restore/_zstd_pure.py`).
8. **Verify** the decrypted blob's SHA-256 equals the blob ID
   (`_read_blob()`).

**Expected outcome:** Plaintext file content or tree JSON, with
integrity proven by MAC + SHA-256.

**Variant axes that apply:**

- Repo format: v1 (no compression-type prefix) and v2 (zstd-capable)
  (`_decrypt_file()`).
- Compression: `zstandard` package when installed, else the bundled
  pure-Python decoder (`_decompress_zstd()` /
  `src/lcsas/restore/_zstd_pure.py`).
- Crypto primitives: all pure-Python, no `cryptography` / OpenSSL
  required.

**Test coverage:**

- Existing: `tests/unit/test_aes_pure.py::TestAESKeySchedule`,
  `TestAESEncryptBlock`, `TestAESCTR` — key schedule,
  AES-128/256 ECB NIST vectors, CTR round-trip, NIST CTR vector,
  empty, partial block, multi-block.
- Existing: `tests/unit/test_restic_fallback.py::TestPoly1305` —
  Poly1305 RFC 8439 vector, empty message, `_clamp_r` bit clearing.
- Existing: `tests/unit/test_restic_fallback.py::TestAuthenticatedEncryption` —
  authenticated encryption round-trip, wrong-key rejection,
  tampered-data rejection, too-short-data rejection, large-data
  round-trip.
- Existing: `tests/unit/test_restic_fallback.py::TestConstantTimeEq` —
  constant-time equality.
- Existing: `tests/unit/test_restic_fallback.py::TestParseTimestamp` —
  timestamp parsing (nanosecond, microsecond, no-fractional).
- Existing: `tests/unit/test_restic_fallback.py::TestKeyDerivation` —
  scrypt key derivation, wrong-password rejection (synthetic key).
- Existing: `tests/integration/test_pure_python_restore.py::TestPurePythonFallbackRestore`
  exercises the full crypto chain against real rustic-produced data
  (scrypt → master key → AES-CTR → Poly1305 → optional zstd → SHA-256).

**Source refs:**
module docstring crypto-stack table, `_poly1305_mac()`,
`_decrypt_authenticated()`, `_load_master_key()`, `_try_keys()`
(KDF + AE), `_decrypt_file()` / `_load_index()` / `_load_snapshots()`
(file/index/snapshot decryption), `_read_blob()` (blob read + verify) —
all in `src/lcsas/restore/restic_fallback.py`;
`src/lcsas/restore/_aes_pure.py`;
`tests/unit/test_aes_pure.py`.

---

## Workflow F — Recover a single file vs a full snapshot

**Purpose:** Match the recovery scope to the urgency. Tier 3 supports
both, but the pure-Python tree traversal is recursive — there is no
built-in "extract one path" flag.

**Prerequisites:** Workflow B prerequisites.

### F.1 — Full snapshot restore (the default)

**Steps:**

1. Build the cache directory (Workflow B step 1).
2. Run the restorer with `--target /path/to/output` and either no
   `--snapshot` (latest) or a hex prefix
   (`PurePythonRestorer.restore()` in
   `src/lcsas/restore/restic_fallback.py`).
3. The restorer recursively walks the snapshot's root tree
   (`restore()` → `_restore_tree()`), reconstructing
   directories, files, symlinks (validated against path traversal
   inside `_restore_tree()`), and hardlinks.
   Metadata (mode, mtime, atime, xattrs) is restored
   best effort (`_apply_metadata()`).

**Expected outcome:** A complete tree under `--target` matching the
snapshot, byte-for-byte identical to a `rustic restore` of the same
snapshot — proven by
`tests/integration/test_pure_python_restore.py::TestPurePythonFallbackRestore::test_fallback_matches_rustic_restore`.

### F.2 — Single-file recovery

There is **no** `--include` flag on the standalone restorer
(`_CLI_BLOCK` in `src/lcsas/restore/standalone_builder.py`). Two
practical options:

1. **Restore the whole snapshot to scratch space**, then copy the one
   file out. Acceptable when the snapshot is small relative to free
   space. Use `--list-snapshots`
   (`--list-snapshots` in `_CLI_BLOCK`) first to pick
   the right one.
2. **Drive `PurePythonRestorer` from a Python REPL or short script**:
   import the class from the on-disc `standalone_restorer.py`
   (or from `lcsas_src/restore/restic_fallback.py` on the disc —
   bundled by `HolographicInjector.write_lcsas_source()`), load the
   master key, walk
   `tree → subtree → ... → node`, and read only the desired file's
   content blobs via the private `_read_blob` API
   (`PurePythonRestorer._read_blob()`). Each file's
   content is the concatenation of its `content` blob IDs
   (`PurePythonRestorer._restore_file()`).

**Expected outcome:** Targeted file extracted with the same integrity
guarantees as a full restore (SHA-256 verified per blob).

**Variant axes that apply:**

- OS: any Python-capable host.
- Recovery tier: 3.
- Scope: full snapshot (supported directly) or single-file (manual
  via Python API).

**Test coverage:**

- Existing: full-restore correctness —
  `tests/integration/test_pure_python_restore.py::TestPurePythonFallbackRestore::test_fallback_restore_family`,
  `test_fallback_restore_work`, `test_fallback_matches_rustic_restore`,
  `test_fallback_restore_with_flat_layout`,
  `test_fallback_incremental_files_present`.
- Existing: snapshot listing —
  `...::test_fallback_lists_snapshots`.
- Existing: snapshot lookup by ID prefix —
  `PurePythonRestorer._find_snapshot()` (covered indirectly
  by the restore tests).
- Gap: no test or CLI flag for a single-file extraction. This is
  intentional — Tier 3 is "get everything back, slowly".

**Source refs:**
`PurePythonRestorer.restore()`, `_restore_tree()`, `_restore_file()`,
`_apply_metadata()` in `src/lcsas/restore/restic_fallback.py`,
`_CLI_BLOCK` in `src/lcsas/restore/standalone_builder.py`,
`TestPurePythonFallbackRestore` in
`tests/integration/test_pure_python_restore.py`.

---

## Hard limits of Tier 3

This path deliberately trades capability for portability. It **cannot**:

- **Stitch packs across discs unattended.** `PurePythonRestorer` reads
  packs from its configured search paths — by default the single
  assembled cache directory (`PurePythonRestorer.__init__()`).
  Multi-disc coverage needs an operator in the loop: merge every
  disc's `data/` into the cache beforehand, pass one `--mount-point`
  per disc, or answer the interactive disc-swap prompt when a pack is
  not found (`_find_pack_path()`, added in #234). There is no
  automated pick-list orchestration like the rustic paths have; the
  `lcsas restore standalone` wrapper likewise symlinks only the
  mounted disc's `data/` and instructs the operator to merge other
  discs manually (`cmd_restore_from_disc()` in
  `src/lcsas/cli/main.py`).
- **Locate missing packs by itself (non-interactive).** In
  non-interactive mode a required pack absent from every search path
  raises `FileNotFoundError` (`_find_pack_path()`); under tolerant
  traversal the affected file is recorded as failed. Interactively,
  the disc-swap prompt resolves the missing pack hash to volume
  label(s) via the holographic catalog when one is reachable
  (`_lookup_volume_labels()` / `_discover_catalog()`). Consult
  `catalog.db` (Workflow D) to discover where a missing pack lives
  and mount that disc.
- **Deduplicate or prune.** It is a *reader*, not a repo manager — no
  `forget`, `prune`, or `repair-index` equivalents exist in
  `restic_fallback.py`.
- **Re-encrypt or rotate keys.** No write path through the master key
  exists.
- **Verify ECC.** Read errors on a degraded disc must be repaired with
  `dvdisaster` *before* Tier 3 starts, because the restorer reads
  pack bytes verbatim and verifies SHA-256 afterwards — a flipped bit
  surfaces as an `IntegrityError` from
  `PurePythonRestorer._read_blob()` (recorded as a per-file failure
  under the default tolerant mode).
- **Run fast.** Expect ~1 MB/s on modern hardware
  (module docstring, `src/lcsas/restore/restic_fallback.py`).
- **Handle a repository where the password is wrong.** Wrong password
  raises `IntegrityError` from MAC verification
  (`_decrypt_authenticated()` / `_try_keys()`); use
  `--info`/`verify_key()` to test
  (`PurePythonRestorer.verify_key()`).
- **Recover a damaged blob.** A MAC/SHA-256/zstd failure on a blob
  cannot be repaired at this tier. Under the default *tolerant*
  traversal (#374) the affected file is skipped and listed in
  `RESTORE_FAILURES.txt` under the target (the CLI exits `2`; the
  rest of the data restores); `strict=True` on the Python API
  restores the legacy abort-on-first-error contract
  (`PurePythonRestorer` `strict` parameter,
  `_write_failure_manifest()`).

If you need cross-disc reconstruction or pack repair, escalate to
Tier 1 or Tier 2 (`docs/workflows/restore-host-linux.md`,
`docs/workflows/meta-volume.md`) — both still leverage the same
holographic catalog this tier relies on.

---

## Test coverage matrix

| Concern | Test | Status |
| --- | --- | --- |
| AES-128/256 key schedule (FIPS 197) | `tests/unit/test_aes_pure.py::TestAESKeySchedule` | Covered |
| AES-128 ECB NIST vector | `tests/unit/test_aes_pure.py::TestAESEncryptBlock` | Covered |
| AES-256 ECB NIST vector | `tests/unit/test_aes_pure.py::TestAESEncryptBlock` | Covered |
| AES-CTR round-trip + NIST | `tests/unit/test_aes_pure.py::TestAESCTR` | Covered |
| Poly1305 RFC 8439 vector | `tests/unit/test_restic_fallback.py::TestPoly1305` | Covered |
| `_clamp_r` bit clearing | `tests/unit/test_restic_fallback.py::TestPoly1305` | Covered |
| Authenticated encryption round-trip | `tests/unit/test_restic_fallback.py::TestAuthenticatedEncryption` | Covered |
| Constant-time equality | `tests/unit/test_restic_fallback.py::TestConstantTimeEq` | Covered |
| Timestamp parsing | `tests/unit/test_restic_fallback.py::TestParseTimestamp` | Covered |
| scrypt master-key derivation (synthetic) | `tests/unit/test_restic_fallback.py::TestKeyDerivation` | Covered |
| `PurePythonRestorer` core methods (synthetic) | `tests/unit/test_restic_fallback.py::TestPurePythonRestorer` | Covered |
| Permission / flat-layout / symlink / hardlink / xattr restore | `tests/unit/test_restic_fallback.py` (later classes) | Covered |
| scrypt → master key (real rustic) | `tests/integration/test_pure_python_restore.py::TestPurePythonFallbackRestore::test_fallback_verifies_key` | Covered |
| Wrong-password rejection (real rustic) | `...::test_fallback_rejects_wrong_password` | Covered |
| Snapshot listing | `...::test_fallback_lists_snapshots` | Covered |
| Repo info (version, blob count) | `...::test_fallback_repo_info` | Covered |
| Full restore (family — initial + incremental) | `...::test_fallback_restore_family` | Covered |
| Full restore (work — modified files) | `...::test_fallback_restore_work` | Covered |
| Pure-Python ≡ rustic byte-for-byte | `...::test_fallback_matches_rustic_restore` | Covered |
| Flat (legacy) pack layout | `...::test_fallback_restore_with_flat_layout` | Covered |
| Incremental file presence | `...::test_fallback_incremental_files_present` | Covered |
| `RestoreExecutor.prepare_cache` + fallback | `...::test_restore_executor_with_fallback_pipeline` | Covered |
| Every ISO is self-describing (catalog + volume_info) | `tests/integration/test_disc_only_restore.py::TestDiscOnlyRestore::test_isos_are_self_describing` | Covered |
| Holographic metadata on every ISO | `...::test_every_iso_has_holographic_metadata` | Covered |
| Latest catalog knows all volumes | `...::test_latest_catalog_knows_all_volumes` | Covered |
| Pick list from on-disc catalog | `...::test_on_disc_catalog_enables_pick_list` | Covered |
| Packs span multiple discs | `...::test_packs_span_multiple_discs` | Covered |
| `cmd_restore_from_disc` argparser registration | `tests/unit/test_restore_from_disc.py::TestFromDiscParser` | Covered |
| `cmd_restore_from_disc` validation paths | `tests/unit/test_restore_from_disc.py::TestFromDiscValidation` | Covered |
| `cmd_restore_from_disc` batch mode | `tests/unit/test_restore_from_disc.py::TestFromDiscBatchMode` | Covered |
| `cmd_restore_from_disc` pure-Python branch E2E | — | **Gap** |
| Generated `standalone_restorer.py` run as subprocess | — | **Gap** |
| `volume_info.json` shape | — | **Gap** (existence only) |
| Single-file extraction API | — | **Gap** (by design — not exposed) |

---

## Consolidated source refs

Required reading (repo-relative):

- `CLAUDE.md`
- `src/lcsas/cli/main.py` — the `restore standalone` subparser in
  `build_parser()`, handler `cmd_restore_from_disc()`, dispatch
  `dispatch()`.
- `src/lcsas/restore/restic_fallback.py` — pure-Python AES/zstd reader
  (full file).
- `src/lcsas/restore/_aes_pure.py` — AES primitives.
- `src/lcsas/restore/_zstd_pure.py` — pure-Python zstd decoder
  (RST-04 fallback).
- `src/lcsas/restore/standalone_builder.py` — `build_standalone()` +
  `_CLI_BLOCK` build `standalone_restorer.py`.
- `src/lcsas/staging/metadata.py` — `HolographicInjector`
  (class), `write_standalone_restorer()`, `write_lcsas_source()`,
  `write_restore_instructions()`, `inject_catalog()`,
  `write_volume_info()`.
- `src/lcsas/restore/executor.py` — `prepare_cache()`,
  `ingest_volume()`, `verify_cache_completeness()`.
- `tests/unit/test_aes_pure.py` — AES-CTR NIST vectors.
- `tests/integration/test_disc_only_restore.py` — end-to-end
  rustic-path multi-disc proof.
- `tests/integration/test_pure_python_restore.py` — end-to-end
  fallback-path proof.
