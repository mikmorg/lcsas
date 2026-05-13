# Backup & Catalog Scan

Backup and scan are the **HOT-tier entry points** of the LCSAS pipeline. Source
data first lands in a managed **Rustic repository** on local/NAS disk (Tier 0 —
HOT) by way of `rustic backup`. LCSAS itself does not invoke `rustic backup` for
production data; it expects the operator (or an external cron job / pre-existing
backup script) to drive Rustic directly so that pack files appear under
`<mirror_path>/data/`. Once packs exist on disk, `lcsas scan` walks the mirror,
diffs the result against the SQLite catalog, registers new packs, persists
snapshot metadata, and optionally reconciles packs that `rustic prune` has
removed from the mirror.

Everything downstream — bin-packing into volumes, staging ISOs into the WARM
tier, ECC augmentation, and ultimately burning to the COLD tier — operates on
the rows scanned into the `packs` table. If `scan` does not register a pack,
that pack is invisible to the burn pipeline.

## Table of contents

1. [Running a rustic backup against a managed repo](#1-running-a-rustic-backup-against-a-managed-repo)
2. [`lcsas scan` — full scan across all configured repos](#2-lcsas-scan--full-scan-across-all-configured-repos)
3. [`lcsas scan --repo <name>` — single-repo filter](#3-lcsas-scan---repo-name--single-repo-filter)
4. [`lcsas scan --no-snapshots` — skip rustic snapshot listing](#4-lcsas-scan---no-snapshots--skip-rustic-snapshot-listing)
5. [`lcsas scan --no-prune-sync` — skip prune reconciliation](#5-lcsas-scan---no-prune-sync--skip-prune-reconciliation)
6. [Pack registration & delta computation (internals)](#6-pack-registration--delta-computation-internals)
7. [Gaps & known issues](#7-gaps--known-issues)

---

## 1. Running a rustic backup against a managed repo

**Purpose:** Produce new pack files in the HOT-tier mirror so LCSAS has
something to scan and eventually burn. LCSAS does not wrap `rustic backup` from
the CLI; it expects the operator to run Rustic directly (or via a Protocol
backup runner in tests/programmatic use).

**Prerequisites:**
- A registered repo in the catalog (`lcsas repo add <name> <mirror_path>`)
  (`src/lcsas/cli/main.py:79`).
- An initialized Rustic repository at `mirror_path` with a `data/`,
  `index/`, `keys/`, `snapshots/`, and `config` layout. `SubprocessRusticRunner.init_repo`
  can do this programmatically (`src/lcsas/rustic/wrapper.py:120`).
- A `password_file` pointing at the repository's encryption key
  (referenced by `scan` for snapshot listing — see
  `src/lcsas/cli/main.py:706` and `src/lcsas/cli/main.py:722`).
- `rustic >= 0.9.0` on PATH (enforced at scan time by
  `check_binary_version("rustic", min_version=(0, 9, 0))` —
  `src/lcsas/cli/main.py:695`).

**Steps:**
1. `rustic -r <mirror_path> --password-file <pwfile> init` — one-time, only
   if the repo does not exist (`src/lcsas/rustic/wrapper.py:126`).
2. `rustic -r <mirror_path> --password-file <pwfile> backup --json <src...>` —
   produce a new snapshot and the pack files that back it. The expected
   `--json` envelope is what `SubprocessRusticRunner.backup` issues
   (`src/lcsas/rustic/wrapper.py:131`).
3. (Optional) `rustic -r <mirror_path> --password-file <pwfile> prune` — drops
   unreferenced packs from the local mirror; `scan` will reconcile them
   (see workflow §5).

**Expected outcome:**
- New pack files appear under `<mirror_path>/data/`. LCSAS supports both
  layouts:
  - Flat: `data/<64-hex-sha256>` (`src/lcsas/packs/scanner.py:64`).
  - Two-level: `data/ab/abcdef...` (`src/lcsas/packs/scanner.py:67`).
- A new entry under `<mirror_path>/snapshots/` describing the snapshot, which
  `scan` will harvest via `rustic snapshots --json`
  (`src/lcsas/rustic/wrapper.py:141`).
- File names are 64-character lowercase hex (the SHA-256 of the pack); other
  names are skipped by the scanner regex `^[0-9a-f]{64}$`
  (`src/lcsas/packs/scanner.py:13`).
- Zero-byte files are skipped with a warning — treated as an incomplete
  write rather than a real pack (`src/lcsas/packs/scanner.py:28`).

**Variant axes that apply:** Multi-tenant (one repo per tenant, distinct
password files). All other axes: N/A — backup is upstream of media selection,
ECC, and burn.

**Test coverage:**
- `tests/unit/test_rustic_wrapper.py` exercises the Protocol wrapper with a
  fake subprocess runner.
- `tests/unit/test_rustic_parser.py` covers the JSON envelope parsing.
- Integration: no end-to-end test currently drives a real `rustic backup`
  against a temp repo as part of the unit suite (integration tests are gated
  on `rustic` being on PATH).

**Source refs:**
- `src/lcsas/rustic/wrapper.py:16` (Protocol) · `:64` (subprocess impl) ·
  `:120` (init) · `:131` (backup) · `:141` (snapshots) · `:170` (prune).
- `src/lcsas/rustic/types.py:8` (`BackupResult`) · `:20` (`SnapshotInfo`).
- `src/lcsas/packs/scanner.py:13` (pack-name regex).

---

## 2. `lcsas scan` — full scan across all configured repos

**Purpose:** Walk every configured mirror, register any new pack files in the
SQLite catalog, mark mirror-absent packs as pruned, and persist the current
snapshot list per repo. This is the canonical "what is on disk that I haven't
catalogued yet?" command.

**Prerequisites:**
- A TOML config file with `[paths]` and one or more `[repos.<name>]` blocks.
  `--config` is **required** for scan (`src/lcsas/cli/main.py:623`).
- A catalog DB. Path resolution order: `--db` flag > `config.db_path`
  (`src/lcsas/cli/main.py:629`). Schema is created on demand by
  `create_all(conn)` (`src/lcsas/cli/main.py:630`).
- Each repo in config must already exist in the DB (run
  `lcsas repo add <name> <mirror_path>` first); unregistered repos are
  warned and skipped (`src/lcsas/cli/main.py:649-655`).
- For snapshot persistence: `password_file` must be set per repo
  (`src/lcsas/cli/main.py:706`) and `rustic >= 0.9.0` must be on PATH
  (`src/lcsas/cli/main.py:695`).

**Steps:**
1. `lcsas --config <conf.toml> [--db <path>] scan` — argparse entry
   (`src/lcsas/cli/main.py:93`), dispatched to `cmd_scan`
   (`src/lcsas/cli/main.py:611`).
2. For each repo in `config.repositories` (`src/lcsas/cli/main.py:645`):
   a. Walk `mirror_path/data/` via `scan_mirror_packs`
      (`src/lcsas/cli/main.py:658`, `src/lcsas/packs/scanner.py:37`).
   b. Diff against the catalog with `DeltaAnalyzer.register_new_packs`
      (`src/lcsas/cli/main.py:661-662`, `src/lcsas/packs/delta.py:31`).
   c. Compute unarchived totals via `get_unarchived` /
      `get_total_unarchived_bytes` (`src/lcsas/cli/main.py:663-664`,
      `src/lcsas/packs/delta.py:73`).
   d. Reconcile pruned packs via `detect_pruned` + `bulk_mark_pruned`
      (`src/lcsas/cli/main.py:669-679`, `src/lcsas/packs/delta.py:85`,
      `src/lcsas/db/packs.py:79`).
3. Persist snapshots: `rustic snapshots --json` per repo, then
   `bulk_upsert_snapshots` (`src/lcsas/cli/main.py:687-743`,
   `src/lcsas/rustic/wrapper.py:141`).
4. Print archive summary via `get_archive_status_summary`
   (`src/lcsas/cli/main.py:748`).

**Expected outcome:**
- New packs appear in the `packs` table with `is_pruned = 0`, sized from
  `stat().st_size` at scan time (`src/lcsas/db/packs.py:100`).
- Already-known packs are not re-inserted; `INSERT OR IGNORE` makes the
  command safe to re-run (`src/lcsas/db/packs.py:121`).
- Packs in the DB but missing from the mirror are flagged as pruned (unless
  `--no-prune-sync`); their `is_pruned` flag flips to 1.
- Snapshots are upserted into the `snapshots` table.
- stdout per repo:
  ```
  <name>:
    Packs on disk:  N
    Newly registered: M
    Unarchived:     U (B bytes)
  ```
  and a footer:
  ```
  Total scanned: N packs across R repos
  New packs registered: M
  Archive: T total, A archived, U unarchived
  ```
  (`src/lcsas/cli/main.py:681-684`, `:750-755`).

**Variant axes that apply:** Multi-tenant (loops over all repos in config).
Other axes: N/A.

**Test coverage:**
- `tests/unit/test_cli_scan.py::TestCmdScan::test_scan_discovers_new_packs`
  end-to-end with a fake mirror.
- `test_scan_idempotent` — second run registers zero new packs.
- `test_scan_empty_mirror` — graceful handling of an empty `data/`.
- `test_scan_prints_total_summary` — footer formatting.
- Scanner specifics: `tests/unit/test_scanner_delta.py::TestScanner`
  (two-level layout, flat layout, missing `data/`, permission errors).
- **Gap:** No unit test exercises the snapshot-persistence branch
  (`src/lcsas/cli/main.py:687-746`); the test config sets
  `password_file = ""` to skip that branch
  (`tests/unit/test_cli_scan.py:37`, `src/lcsas/cli/main.py:706`).
- **Gap:** No test covers the `rustic` binary-version check failure path
  (`src/lcsas/cli/main.py:694-698`).

**Source refs:**
- CLI: `src/lcsas/cli/main.py:92-108` (parser) · `:611-756` (`cmd_scan`) ·
  `:2671-2672` (dispatch).
- Scanner: `src/lcsas/packs/scanner.py:37`.
- Delta: `src/lcsas/packs/delta.py:15`.
- Catalog: `src/lcsas/db/packs.py:100` (`bulk_register`) · `:79`
  (`bulk_mark_pruned`).

---

## 3. `lcsas scan --repo <name>` — single-repo filter

**Purpose:** Limit a scan to one or more named repositories. Useful when one
mirror is slow, network-mounted, or has just received a big backup batch.

**Prerequisites:** Same as the full scan, plus the supplied repo name(s) must
exist in `config.repositories`. Unknown names trigger a warning and are
skipped (`src/lcsas/cli/main.py:640-643`).

**Steps:**
1. `lcsas --config <conf.toml> scan --repo family` — single repo.
2. `lcsas --config <conf.toml> scan --repo family personal work` — multiple
   repos (`--repo` is `nargs="*"`, `src/lcsas/cli/main.py:98`).
3. The handler builds `repo_filter = set(args.repo)`
   (`src/lcsas/cli/main.py:635`) and skips repos whose name is not in the
   filter at both the pack-scan loop (`src/lcsas/cli/main.py:646`) and the
   snapshot-persistence loop (`src/lcsas/cli/main.py:704`).

**Expected outcome:**
- Only the named repo(s) are walked; other repos' packs and snapshots are
  untouched.
- The footer still reports `across R repos` where R is `len(config.repositories)`
  — i.e., the **configured** total, not the filtered count
  (`src/lcsas/cli/main.py:751`). This is mildly misleading; see Gaps §7.
- Unknown repo names emit `"repository '<name>' not found in config, skipping."`
  (`src/lcsas/cli/main.py:643`).

**Variant axes that apply:** Multi-tenant (this *is* the per-tenant axis).
Other axes: N/A.

**Test coverage:**
- `tests/unit/test_cli_scan.py::TestCmdScan::test_scan_repo_filter`
  verifies only the named repo is scanned and only its packs are registered.
- `tests/unit/test_cli_scan.py::TestScanParser::test_scan_parser_with_repo_filter`
  covers argparse acceptance of multiple names.
- **Gap:** No test covers the "unknown repo name" warning path
  (`src/lcsas/cli/main.py:640-643`).

**Source refs:** `src/lcsas/cli/main.py:97-100` (flag) · `:635-655` (filter
application) · `:704-705` (filter for snapshots).

---

## 4. `lcsas scan --no-snapshots` — skip rustic snapshot listing

**Purpose:** Skip the per-repo `rustic snapshots --json` step. Useful when
rustic is slow, the password file is not available, or the operator only
wants to refresh the pack catalog. Note the **flag name in code is
`--no-snapshots`** (not `--skip-snapshots`), and the spec doc/task description
should be read accordingly.

**Prerequisites:** Same as a full scan, minus the `password_file` and the
rustic-on-PATH requirement (both checked inside the snapshot branch only).

**Steps:**
1. `lcsas --config <conf.toml> scan --no-snapshots` — parser flag
   (`src/lcsas/cli/main.py:101-104`).
2. `cmd_scan` evaluates `if not getattr(args, "no_snapshots", False)` and
   skips the entire snapshot block when the flag is set
   (`src/lcsas/cli/main.py:687`).

**Expected outcome:**
- The packs table is updated as in the full scan.
- The `snapshots` table is **not** touched. Existing snapshot rows are
  preserved as-is (they are not invalidated, since they may still describe
  packs already on burned media).
- The `rustic` binary-version check (`src/lcsas/cli/main.py:694-698`) is
  bypassed — `scan --no-snapshots` works on a host with no rustic installed.
- No "Snapshots persisted: N" line is printed
  (`src/lcsas/cli/main.py:745-746`).

**Variant axes that apply:** Multi-tenant. Other axes: N/A.

**Test coverage:**
- Indirectly covered: the test fixture sets `password_file = ""` which
  triggers the same skip path inside the snapshot branch
  (`tests/unit/test_cli_scan.py:37`, `src/lcsas/cli/main.py:706-711`).
- **Gap:** No dedicated test passes `--no-snapshots` explicitly.

**Source refs:** `src/lcsas/cli/main.py:101-104` (flag) · `:687`
(guard) · `:687-746` (snapshot block being skipped).

---

## 5. `lcsas scan --no-prune-sync` — skip prune reconciliation

**Purpose:** Disable the "detect packs absent from the mirror and mark them
as pruned" step. Use when the mirror is known to be incomplete (e.g., still
syncing from a remote NAS) so as not to flip live packs to `is_pruned = 1`
spuriously.

**Prerequisites:** Same as a full scan.

**Steps:**
1. `lcsas --config <conf.toml> scan --no-prune-sync` — parser flag
   (`src/lcsas/cli/main.py:105-108`).
2. `cmd_scan` guards the prune-sync block with
   `if not getattr(args, "no_prune_sync", False)` and skips
   `DeltaAnalyzer.detect_pruned` + `bulk_mark_pruned`
   (`src/lcsas/cli/main.py:669-679`).

**Expected outcome:**
- New packs are still registered.
- Packs in the DB that no longer exist on the mirror **keep**
  `is_pruned = 0`.
- No `"Pruned packs: N (B bytes)"` line is printed.
- Note that even without `--no-prune-sync`, an **empty** scanner result
  (e.g., totally unreachable mirror) is already treated as "cannot detect
  pruned packs" with a warning, not as "every pack is pruned"
  (`src/lcsas/packs/delta.py:96-104`) — the flag exists for the case where
  the mirror *partially* exists.

**Variant axes that apply:** Multi-tenant. Other axes: N/A.

**Test coverage:**
- `DeltaAnalyzer.detect_pruned` itself is covered:
  `tests/unit/test_scanner_delta.py::TestDeltaAnalyzer::test_detect_pruned_finds_missing`,
  `::test_detect_pruned_empty_scanner`,
  `::test_detect_pruned_ignores_already_pruned`.
- `bulk_mark_pruned` is covered:
  `tests/unit/test_scanner_delta.py::TestBulkMarkPruned`.
- **Gap:** No CLI-level test exercises `--no-prune-sync`; no test asserts
  the prune-sync block runs in a default scan and updates `is_pruned`.

**Source refs:** `src/lcsas/cli/main.py:105-108` (flag) · `:669-679`
(guarded block) · `src/lcsas/packs/delta.py:85` (`detect_pruned`) ·
`src/lcsas/db/packs.py:79` (`bulk_mark_pruned`).

---

## 6. Pack registration & delta computation (internals)

**Purpose:** Document the algorithm that turns a directory listing into
catalog rows. This is the load-bearing piece of every scan invocation; it
also runs implicitly inside `cmd_stage`, `cmd_burn`, and related pipeline
commands when they instantiate a `DeltaAnalyzer`.

**Prerequisites:** A `dict[str, int]` from `scan_mirror_packs` mapping
SHA-256 filename to byte size (`src/lcsas/packs/scanner.py:37`).

**Algorithm (`DeltaAnalyzer.register_new_packs`,
`src/lcsas/packs/delta.py:31`):**
1. If the scanner returned an empty dict, return `[]` immediately
   (`src/lcsas/packs/delta.py:40-41`).
2. Reject if `repo_id` was not supplied at construction time — packs must
   be tied to a repo (`src/lcsas/packs/delta.py:43-47`).
3. Build `(sha256, size_bytes, repo_id)` tuples for every scanner entry
   (`src/lcsas/packs/delta.py:49-52`).
4. Batch-query existing SHA-256s in chunks of `_batch = 900` to stay
   below SQLite's `SQLITE_MAX_VARIABLE_NUMBER`
   (`src/lcsas/packs/delta.py:55-65`, parallels `_SQLITE_BATCH = 900` in
   `src/lcsas/db/packs.py:14`).
5. Filter to "not yet in DB" and call `bulk_register`
   (`src/lcsas/packs/delta.py:67-71`).
6. `bulk_register` uses `INSERT OR IGNORE … executemany` followed by a
   batched `SELECT` to return Pack rows; it logs a warning if the DB-side
   size differs from the on-disk size for an already-present pack
   (`src/lcsas/db/packs.py:121-145`).

**Prune detection (`DeltaAnalyzer.detect_pruned`,
`src/lcsas/packs/delta.py:85`):**
1. `list_packs(conn, repo_id, include_pruned=False)` fetches active packs
   for this repo (`src/lcsas/packs/delta.py:94`, `src/lcsas/db/packs.py:149`).
2. If the scanner returned nothing, *bail out* with a warning rather than
   marking every active pack as pruned — this is the "is the mirror path
   right?" guard (`src/lcsas/packs/delta.py:96-104`).
3. Otherwise return active packs whose SHA-256 is not in the scanner result
   (`src/lcsas/packs/delta.py:106-107`).
4. `cmd_scan` then runs `bulk_mark_pruned` over the returned pack IDs
   (`src/lcsas/cli/main.py:672-675`, `src/lcsas/db/packs.py:79`).

**Expected outcome:**
- A pack on disk is registered exactly once, regardless of how many times
  scan is re-run (`INSERT OR IGNORE`).
- A pack absent from disk but in the DB is flipped to `is_pruned = 1` —
  unless `--no-prune-sync` is set, or the mirror is completely empty.
- Pack size in the DB is whatever the **first** scan recorded; subsequent
  scans only log a warning on mismatch (`src/lcsas/db/packs.py:138-145`).
  This is intentional because pack SHA-256 is content-addressed, so size
  cannot legitimately change without a new hash.

**Variant axes that apply:** Multi-tenant (each repo has its own `repo_id`,
and the delta is computed per repo). Other axes: N/A.

**Test coverage:**
- `tests/unit/test_scanner_delta.py::TestDeltaAnalyzer` — register-new,
  skip-existing, unarchived totals, repo filtering, pruned detection.
- `tests/unit/test_db_packs.py` — CRUD operations on the packs table.
- **Gap:** No test exercises the SQLite-variable-batching path with
  >900 packs in a single scan.
- **Gap:** No test asserts the size-mismatch warning in `bulk_register`
  (`src/lcsas/db/packs.py:138-145`).

**Source refs:**
- `src/lcsas/packs/delta.py:15` (`DeltaAnalyzer` class) · `:31`
  (`register_new_packs`) · `:85` (`detect_pruned`).
- `src/lcsas/db/packs.py:100` (`bulk_register`) · `:79`
  (`bulk_mark_pruned`) · `:149` (`list_packs`).
- `src/lcsas/packs/scanner.py:37` (`scan_mirror_packs`).

---

## 7. Gaps & known issues

- **Misleading footer when `--repo` filters.** The total-scanned line says
  `across {len(config.repositories)} repos` even when `--repo` narrows the
  scan to one repo (`src/lcsas/cli/main.py:751`). A filtered scan that
  visits one repo out of five will still print "across 5 repos". Cosmetic
  only.
- **No `--no-snapshots` CLI test.** The flag's skip path is only exercised
  indirectly (via empty `password_file`). A direct test would protect the
  current behaviour.
- **No `--no-prune-sync` CLI test.** Same as above for the prune-sync
  guard.
- **Size mismatch is non-fatal.** `bulk_register` logs a warning on size
  mismatch but trusts the DB row (`src/lcsas/db/packs.py:138-145`). For a
  content-addressed store this is the conservative choice, but no
  observability surface (DB column, audit event) flags that a mismatch was
  seen. Operators only see a log line.
- **`detect_pruned` semantics when the mirror is *partly* missing.** If
  the mirror returns *some* packs but not all (e.g., a partial NFS mount),
  the missing ones will be flagged as pruned with no further check. Use
  `--no-prune-sync` whenever a mirror's completeness is uncertain.
- **Snapshot listing failure is per-repo soft-fail.** If `rustic snapshots`
  raises for a given repo, the error is logged and the loop continues
  (`src/lcsas/cli/main.py:725-729`). The overall scan still returns 0,
  which can mask partial outages. No metric or audit-trail event is
  emitted.
- **No integration test driving real `rustic backup`** as the upstream
  event of a scan. Existing tests fabricate pack files directly on disk.

---

*Document generated as part of the LCSAS workflow matrix — see
`docs/workflows/` for sibling docs covering bin-packing, staging, ISO
mastering, ECC, burning, restoration, consolidation, and meta-volume
workflows.*
