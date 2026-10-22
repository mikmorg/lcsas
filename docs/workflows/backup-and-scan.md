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
5. [`lcsas scan --no-prune-sync` / `--yes-prune` — prune reconciliation controls](#5-lcsas-scan---no-prune-sync----yes-prune--prune-reconciliation-controls)
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
  (`cmd_repo_add` in `src/lcsas/cli/main.py`).
- An initialized Rustic repository at `mirror_path` with a `data/`,
  `index/`, `keys/`, `snapshots/`, and `config` layout.
  `SubprocessRusticRunner.init_repo` can do this programmatically
  (`src/lcsas/rustic/wrapper.py`).
- A `password_file` pointing at the repository's encryption key
  (referenced by `scan` for snapshot listing — see the snapshot block in
  `cmd_scan`, `src/lcsas/cli/main.py`).
- `rustic >= 0.9.0` on PATH (enforced at scan time by
  `check_binary_version("rustic", min_version=(0, 9, 0))` in `cmd_scan`,
  `src/lcsas/cli/main.py`).

**Steps:**
1. `rustic -r <mirror_path> --password-file <pwfile> init` — one-time, only
   if the repo does not exist (`SubprocessRusticRunner.init_repo`,
   `src/lcsas/rustic/wrapper.py`).
2. `rustic -r <mirror_path> --password-file <pwfile> backup --json <src...>` —
   produce a new snapshot and the pack files that back it. The expected
   `--json` envelope is what `SubprocessRusticRunner.backup` issues
   (`src/lcsas/rustic/wrapper.py`).
3. (Optional) `rustic -r <mirror_path> --password-file <pwfile> prune` — drops
   unreferenced packs from the local mirror; `scan` will reconcile them
   (see workflow §5).

**Expected outcome:**
- New pack files appear under `<mirror_path>/data/`. LCSAS supports both
  layouts:
  - Flat: `data/<64-hex-sha256>` (`scan_mirror_packs`, `src/lcsas/packs/scanner.py`).
  - Two-level: `data/ab/abcdef...` (`scan_mirror_packs`, `src/lcsas/packs/scanner.py`).
- A new entry under `<mirror_path>/snapshots/` describing the snapshot, which
  `scan` will harvest via `rustic snapshots --json`
  (`SubprocessRusticRunner.snapshots`, `src/lcsas/rustic/wrapper.py`).
- File names are 64-character lowercase hex (the SHA-256 of the pack); other
  names are skipped by the scanner regex `_PACK_NAME_RE = ^[0-9a-f]{64}$`
  (`src/lcsas/packs/scanner.py`).
- Zero-byte files are skipped with a warning — treated as an incomplete
  write rather than a real pack (`_register_pack`, `src/lcsas/packs/scanner.py`).

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
- `src/lcsas/rustic/wrapper.py`: `RusticRunner` (Protocol) ·
  `SubprocessRusticRunner` (impl) · `init_repo` · `backup` · `snapshots` ·
  `prune_dry_run`.
- `src/lcsas/rustic/types.py`: `BackupResult` · `SnapshotInfo`.
- `src/lcsas/packs/scanner.py`: `_PACK_NAME_RE` (pack-name regex).

---

## 2. `lcsas scan` — full scan across all configured repos

**Purpose:** Walk every configured mirror, register any new pack files in the
SQLite catalog, mark mirror-absent packs as pruned, and persist the current
snapshot list per repo. This is the canonical "what is on disk that I haven't
catalogued yet?" command.

**Prerequisites:**
- A TOML config file with `[paths]` and one or more `[repos.<name>]` blocks.
  `--config` is **required** for scan (`cmd_scan` returns early if it is
  `None`).
- A catalog DB. Path resolution order: `--db` flag > `config.db_path`
  (the `locked_connection(...)` call in `cmd_scan`). Schema is ensured on
  demand by `ensure_schema(conn)`.
- Each repo in config must already exist in the DB (run
  `lcsas repo add <name> <mirror_path>` first); unregistered repos are
  warned and skipped (`cmd_scan`, "not registered in DB" branch).
- For snapshot persistence: `password_file` must be set per repo and
  `rustic >= 0.9.0` must be on PATH (both checked inside the snapshot
  block of `cmd_scan`).

**Steps:**
1. `lcsas --config <conf.toml> [--db <path>] scan` — argparse entry
   (`scan_p` in `build_parser()`), dispatched to `cmd_scan`
   (`src/lcsas/cli/main.py`).
2. For each repo in `config.repositories` (`cmd_scan`):
   a. Walk `mirror_path/data/` via `scan_mirror_packs`, which returns a
      `MirrorScanResult` with `.packs` and `.errors`
      (`scan_mirror_packs` in `src/lcsas/packs/scanner.py`).
   b. Diff against the catalog with `DeltaAnalyzer.register_new_packs`
      (`src/lcsas/packs/delta.py`).
   c. Compute unarchived totals via `get_unarchived` /
      `get_total_unarchived_bytes` (`DeltaAnalyzer`,
      `src/lcsas/packs/delta.py`).
   d. Reconcile pruned packs via `detect_pruned` + `bulk_mark_pruned`,
      but **only when the scan was complete** (`scan_result.errors` empty)
      and only if the mass-prune guard passes (see §5)
      (`cmd_scan`; `DeltaAnalyzer.detect_pruned`, `src/lcsas/packs/delta.py`;
      `bulk_mark_pruned`, `src/lcsas/db/packs.py`).
3. Persist snapshots: `rustic snapshots --json` per repo, then
   `bulk_upsert_snapshots` (snapshot block of `cmd_scan`;
   `SubprocessRusticRunner.snapshots` in `src/lcsas/rustic/wrapper.py`).
4. Print archive summary via `get_archive_status_summary`
   (`src/lcsas/db/queries.py`).

**Expected outcome:**
- New packs appear in the `packs` table with `is_pruned = 0`, sized from
  `stat().st_size` at scan time (`bulk_register`, `src/lcsas/db/packs.py`).
- Already-known packs are not re-inserted; `INSERT OR IGNORE` makes the
  command safe to re-run (`bulk_register`, `src/lcsas/db/packs.py`).
- Packs in the DB but missing from the mirror are flagged as pruned (unless
  `--no-prune-sync`, the scan was incomplete, or the mass-prune guard
  trips — see §5); their `is_pruned` flag flips to 1.
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
  Total scanned: N packs across R repo(s)
  New packs registered: M
  Archive: T total, A archived, S staged, U unarchived
  ```
  (`cmd_scan`).

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
- `tests/unit/test_scan_gaps.py::TestNoSnapshots::test_control_without_flag_checks_version_and_lists_snapshots`
  covers the snapshot-persistence branch of `cmd_scan` with a configured
  `password_file`: the rustic version is checked, the runner's snapshots
  are listed for the repo and persisted (`Snapshots persisted: 1`). The
  runner and version check are patched, so no real rustic is involved.
- `tests/unit/test_scan_gaps.py::TestNoSnapshots::test_rustic_version_check_failure_aborts_scan_with_rc1`
  covers the `rustic` binary-version check failure path (the
  `check_binary_version` call in `cmd_scan`): the scan exits 1, the error
  is printed, the runner is never built and no snapshot rows are written.
- `::test_repo_without_password_file_is_skipped_even_without_flag` (same
  class) pins the skip for a repo with no `password_file`.
- `tests/integration/test_scan_real_repo.py` runs `lcsas scan` against a
  real rustic repo (`rustic init` + backups, `repo add`, `scan`): every pack
  rustic wrote is registered under its SHA-256 name and on-disk size, a
  re-scan registers nothing, a later backup adds only its new packs, and
  every snapshot rustic lists is persisted with its id, host, time, paths
  and tags (requires `rustic`; self-skips otherwise).

**Source refs:**
- CLI: `scan_p` in `build_parser()` (parser) · `cmd_scan` (handler), both
  in `src/lcsas/cli/main.py`.
- Scanner: `scan_mirror_packs` in `src/lcsas/packs/scanner.py`.
- Delta: `DeltaAnalyzer` in `src/lcsas/packs/delta.py`.
- Catalog: `bulk_register` · `bulk_mark_pruned`, both in
  `src/lcsas/db/packs.py`.

---

## 3. `lcsas scan --repo <name>` — single-repo filter

**Purpose:** Limit a scan to one or more named repositories. Useful when one
mirror is slow, network-mounted, or has just received a big backup batch.

**Prerequisites:** Same as the full scan, plus the supplied repo name(s) must
exist in `config.repositories`. Unknown names trigger a warning and are
skipped (`cmd_scan`, "not found in config" branch).

**Steps:**
1. `lcsas --config <conf.toml> scan --repo family` — single repo.
2. `lcsas --config <conf.toml> scan --repo family personal work` — multiple
   repos (`--repo` is `nargs="*"`, `scan_p` in `build_parser()`).
3. The handler builds `repo_filter = set(args.repo) if args.repo else None`
   (`cmd_scan`) and skips repos whose name is not in the filter at both the
   pack-scan loop and the snapshot-persistence loop.

**Expected outcome:**
- Only the named repo(s) are walked; other repos' packs and snapshots are
  untouched.
- The footer reports `across R repo(s)` where R is the number of repos
  actually scanned, not the configured total (#516, `cmd_scan`).
- An unknown repo name is an error: `"repository '<name>' not found in
  config."`, then the configured names, and the command exits 1 before
  scanning anything (#516, `cmd_scan`).

**Variant axes that apply:** Multi-tenant (this *is* the per-tenant axis).
Other axes: N/A.

**Test coverage:**
- `tests/unit/test_cli_scan.py::TestCmdScan::test_scan_repo_filter`
  verifies only the named repo is scanned and only its packs are registered.
- `tests/unit/test_cli_scan.py::TestScanParser::test_scan_parser_with_repo_filter`
  covers argparse acceptance of multiple names.
- `tests/unit/test_cli_comprehensive.py::TestCmdScan::test_scan_unknown_repo_warning`
  covers the "unknown repo name" path: it is an error (#516). The command
  names the unknown repo, lists the configured ones, scans nothing and
  exits 1.

**Source refs:** `scan_p` `--repo` in `build_parser()` (flag) · `cmd_scan`
(filter application, both the pack-scan and snapshot loops).

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
   (`scan_p` in `build_parser()`).
2. `cmd_scan` evaluates `if not getattr(args, "no_snapshots", False)` and
   skips the entire snapshot block when the flag is set.

**Expected outcome:**
- The packs table is updated as in the full scan.
- The `snapshots` table is **not** touched. Existing snapshot rows are
  preserved as-is (they are not invalidated, since they may still describe
  packs already on burned media).
- The `rustic` binary-version check (the `check_binary_version` call in
  `cmd_scan`) is bypassed — `scan --no-snapshots` works on a host with no
  rustic installed.
- No "Snapshots persisted: N" line is printed (`cmd_scan`).

**Variant axes that apply:** Multi-tenant. Other axes: N/A.

**Test coverage:**
- The flag is passed explicitly by `tests/unit/test_cli_scan.py::TestPruneSyncGuards`
  (all three tests), `tests/unit/test_cli_comprehensive.py::TestCmdScan::test_scan_registers_new_packs`
  and `::test_scan_unknown_repo_warning`, and
  `tests/unit/test_cli_e2e.py::TestInitRepoScanPipeline`. On a host
  without rustic a regression that ignored the flag would make those
  scans return 1 at the `check_binary_version` call.
- `tests/unit/test_scan_gaps.py::TestNoSnapshots` asserts the skip itself,
  with a `password_file` configured (otherwise the snapshot loop would skip
  the repo anyway and the flag would be unobservable) and a control run
  without the flag proving the patched seams are live:
  `::test_flag_never_touches_rustic` (version check not called, runner not
  even constructed, no `Snapshots persisted` line, the pack scan still
  runs), `::test_flag_leaves_existing_snapshot_rows_untouched` (a runner
  that would rewrite and add rows changes nothing), and
  `::test_flag_works_when_rustic_check_would_fail` (`scan --no-snapshots`
  exits 0 with no rustic installed).

**Source refs:** `scan_p` `--no-snapshots` in `build_parser()` (flag) ·
`cmd_scan` (`not getattr(args, "no_snapshots", False)` guard and the
snapshot block it skips).

---

## 5. `lcsas scan --no-prune-sync` / `--yes-prune` — prune reconciliation controls

**Purpose:** Two flags govern the "detect packs absent from the mirror and
mark them as pruned" step.

- `--no-prune-sync` disables prune reconciliation entirely. Use when the
  mirror is known to be incomplete (e.g., still syncing from a remote NAS)
  so as not to flip live packs to `is_pruned = 1` spuriously.
- `--yes-prune` *confirms* a mass-prune: it lets a single scan mark more
  than `max(10, 20% of a repo's active packs)` as pruned. Without it,
  `cmd_scan` refuses a mass-prune and warns instead (the usual cause is a
  partially-unavailable mirror, not a real `rustic prune`).

**Prerequisites:** Same as a full scan.

**Steps:**
1. `lcsas --config <conf.toml> scan --no-prune-sync` — parser flag
   (`scan_p` in `build_parser()`).
2. `cmd_scan` guards the prune-sync block with
   `if not getattr(args, "no_prune_sync", False)` and skips
   `DeltaAnalyzer.detect_pruned` + `bulk_mark_pruned` when set.
3. Within the prune-sync block, two further guards apply even when the flag
   is *not* set (BURN-09):
   a. If `scan_result.errors` is non-empty (any unreadable path), the scan
      is treated as INCOMPLETE and prune-sync is skipped for that repo with
      a warning — absence from a partial listing is never taken as evidence
      of pruning.
   b. If `detect_pruned` returns more than `max(10, 0.2 * active_total)`
      packs and `--yes-prune` was not passed, `cmd_scan` refuses to mark
      them and warns; re-run with `--yes-prune` to confirm.

**Expected outcome:**
- New packs are still registered.
- With `--no-prune-sync`: packs in the DB that no longer exist on the
  mirror **keep** `is_pruned = 0`, and no `"Pruned packs: N (B bytes)"`
  line is printed.
- Without any flag, an incomplete scan (`scan_result.errors` non-empty) or
  an over-threshold prune set is skipped with a warning rather than flipping
  live packs to `is_pruned = 1`. `--yes-prune` overrides only the
  over-threshold guard, not the incomplete-scan guard.
- Note that `DeltaAnalyzer.detect_pruned` additionally bails out with a
  warning if the scanner result is *empty* (totally unreachable mirror),
  treating it as "cannot detect pruned packs" rather than "every pack is
  pruned" (`src/lcsas/packs/delta.py`).

**Variant axes that apply:** Multi-tenant. Other axes: N/A.

**Test coverage:**
- `DeltaAnalyzer.detect_pruned` itself is covered:
  `tests/unit/test_scanner_delta.py::TestDeltaAnalyzer::test_detect_pruned_finds_missing`,
  `::test_detect_pruned_empty_scanner`,
  `::test_detect_pruned_ignores_already_pruned`.
- `bulk_mark_pruned` is covered:
  `tests/unit/test_scanner_delta.py::TestBulkMarkPruned`.
- The mass-prune guard and `--yes-prune` override are covered at the CLI
  level in `tests/unit/test_cli_scan.py`.
- `tests/unit/test_cli_scan.py::TestPruneSyncGuards::test_small_prune_still_automatic`
  asserts the prune-sync block runs in a default (sub-threshold) scan and
  flips `is_pruned`; `::test_partial_scan_does_not_mark_pruned` covers the
  incomplete-scan guard.
- `tests/unit/test_scan_gaps.py::TestNoPruneSync` exercises `--no-prune-sync`
  itself (the flag's skip path): packs missing from the mirror stay active,
  with a control run of the same setup that does prune
  (`::test_flag_keeps_packs_missing_from_mirror_active`,
  `::test_control_same_setup_without_flag_does_prune`);
  `DeltaAnalyzer.detect_pruned` is never consulted
  (`::test_flag_means_detect_pruned_is_never_consulted`); the mass-prune
  guard never fires and `--yes-prune` cannot resurrect the skipped block
  (`::test_flag_bypasses_the_mass_prune_guard_too`); new packs are still
  registered (`::test_flag_still_registers_new_packs`).

**Source refs:** `scan_p` `--no-prune-sync` / `--yes-prune` in
`build_parser()` (flags) · `cmd_scan` (guarded prune-sync block) ·
`DeltaAnalyzer.detect_pruned` in `src/lcsas/packs/delta.py` ·
`bulk_mark_pruned` in `src/lcsas/db/packs.py`.

---

## 6. Pack registration & delta computation (internals)

**Purpose:** Document the algorithm that turns a directory listing into
catalog rows. This is the load-bearing piece of every scan invocation; it
also runs implicitly inside `cmd_stage`, `cmd_burn`, and related pipeline
commands when they instantiate a `DeltaAnalyzer`.

**Prerequisites:** A `dict[str, int]` mapping SHA-256 filename to byte size.
This is the `.packs` field of the `MirrorScanResult` returned by
`scan_mirror_packs`; `cmd_scan` passes `scan_result.packs` into the
`DeltaAnalyzer` constructor (`src/lcsas/packs/scanner.py`).

**Algorithm (`DeltaAnalyzer.register_new_packs`, `src/lcsas/packs/delta.py`):**
1. If the scanner result (dict) is empty, return `[]` immediately.
2. Reject if `repo_id` was not supplied at construction time — packs must
   be tied to a repo.
3. Build `(sha256, size_bytes, repo_id)` tuples for every scanner entry.
4. Batch-query existing SHA-256s in chunks of `_batch = 900` to stay
   below SQLite's `SQLITE_MAX_VARIABLE_NUMBER` (parallels `_SQLITE_BATCH`
   in `src/lcsas/db/packs.py`).
5. Filter to "not yet in DB" and call `bulk_register`.
6. `bulk_register` uses `INSERT OR IGNORE … executemany` followed by a
   batched `SELECT` to return Pack rows; it logs a warning if the DB-side
   size differs from the on-disk size for an already-present pack
   (`bulk_register`, `src/lcsas/db/packs.py`).

**Prune detection (`DeltaAnalyzer.detect_pruned`, `src/lcsas/packs/delta.py`):**
1. `list_packs(conn, repo_id, include_pruned=False)` fetches active packs
   for this repo (`list_packs`, `src/lcsas/db/packs.py`).
2. If the scanner returned nothing, *bail out* with a warning rather than
   marking every active pack as pruned — this is the "is the mirror path
   right?" guard.
3. Otherwise return active packs whose SHA-256 is not in the scanner result.
4. `cmd_scan` then applies the incomplete-scan and mass-prune guards (see §5)
   and runs `bulk_mark_pruned` over the surviving pack IDs (`cmd_scan`;
   `bulk_mark_pruned`, `src/lcsas/db/packs.py`).

**Expected outcome:**
- A pack on disk is registered exactly once, regardless of how many times
  scan is re-run (`INSERT OR IGNORE`).
- A pack absent from disk but in the DB is flipped to `is_pruned = 1` —
  unless `--no-prune-sync` is set, the scan was incomplete, the mass-prune
  guard tripped (see §5), or the mirror is completely empty.
- Pack size in the DB is whatever the **first** scan recorded; subsequent
  scans only log a warning on mismatch (`bulk_register`,
  `src/lcsas/db/packs.py`). This is intentional because pack SHA-256 is
  content-addressed, so size cannot legitimately change without a new hash.
  `DeltaAnalyzer.register_new_packs` (step 5) gives the same warning for
  packs already in the catalog, which never reach `bulk_register`, so a
  re-scan flags a pack truncated or altered on the mirror (#524).

**Variant axes that apply:** Multi-tenant (each repo has its own `repo_id`,
and the delta is computed per repo). Other axes: N/A.

**Test coverage:**
- `tests/unit/test_scanner_delta.py::TestDeltaAnalyzer` — register-new,
  skip-existing, unarchived totals, repo filtering, pruned detection.
- `tests/unit/test_db_packs.py` — CRUD operations on the packs table.
- `tests/unit/test_scan_gaps.py::TestBatchingBeyond900` exercises the
  SQLite-variable-batching path with more than 900 packs, at the boundaries
  (899, 900, 901) and over several batches (1800, 1801, 2500) for
  `DeltaAnalyzer.register_new_packs`, `bulk_register`, `bulk_mark_pruned`,
  `get_missing_packs` and `get_pick_list`, and end to end through
  `lcsas scan` (1000 packs registered, then 950 pruned with `--yes-prune`).
  The `sqlite_999_vars` fixture lowers each connection's variable limit to
  the classic 999 and proves the cap is live, because a modern SQLite
  accepts 32 766 variables and an unbatched query would otherwise pass.
- `tests/unit/test_scan_gaps.py::TestBulkRegisterSizeMismatch` asserts the
  size-mismatch warning in `bulk_register` (`src/lcsas/db/packs.py`): it
  warns once naming the pack and both sizes, the catalog keeps the
  first-recorded size, a matching size or a brand-new pack is silent, only
  the mismatching pack is reported, and a mismatch in a later 900-pack
  batch is still detected. Through `lcsas scan`,
  `::test_rescan_after_mirror_pack_changed_size_warns` checks that a re-scan
  warns once for a pack whose size changed on the mirror (#524), and
  `::test_rescan_with_unchanged_sizes_does_not_warn` checks that an
  unchanged re-scan is silent.

**Source refs:**
- `DeltaAnalyzer` (class · `register_new_packs` · `detect_pruned`) in
  `src/lcsas/packs/delta.py`.
- `bulk_register` · `bulk_mark_pruned` · `list_packs` in
  `src/lcsas/db/packs.py`.
- `scan_mirror_packs` in `src/lcsas/packs/scanner.py`.

---

## 7. Gaps & known issues

- (Resolved, #516) The footer counts the repos actually scanned:
  `Total scanned: N packs across M repo(s)`, plus `(K skipped, not
  registered: …)` for configured repos that `repo add` never registered.
  An unknown `--repo` name is an error (exit 1, configured names listed),
  no longer a silent exit 0 (`cmd_scan`;
  `tests/unit/test_cli_comprehensive.py::TestCmdScan::test_scan_unknown_repo_warning`).
- **Size mismatch is non-fatal (intentional: pack names are content
  hashes, so trusting the first-recorded size is the conservative choice).**
  A re-scan logs a warning for every already-registered pack whose size on
  the mirror changed, and the catalog keeps the DB row
  (`DeltaAnalyzer.register_new_packs` in `src/lcsas/packs/delta.py`, #524;
  `bulk_register` in `src/lcsas/db/packs.py` warns the same way). No
  observability surface (DB column, audit event) records that a mismatch was
  seen; operators only see the log line (pinned by
  `tests/unit/test_scan_gaps.py::TestBulkRegisterSizeMismatch::test_rescan_after_mirror_pack_changed_size_warns`).
- **`detect_pruned` semantics when the mirror is *partly* missing
  (intentional: a scan cannot tell "pruned" from "not mounted" below the
  guards).** A partial mirror that raises read errors is now skipped for
  prune-sync (`scan_result.errors` guard), and an over-threshold prune set
  requires `--yes-prune` (BURN-09). But a mirror that reads cleanly yet is
  *silently* missing a sub-threshold set of files will still flag those as
  pruned with no further check. Use `--no-prune-sync` whenever a mirror's
  completeness is uncertain.
- **Snapshot listing failure is per-repo soft-fail.** If `rustic snapshots`
  raises for a given repo, the error is logged and the loop continues
  (snapshot block of `cmd_scan`). The overall scan still returns 0, which
  can mask partial outages. No metric or audit-trail event is emitted, and
  no test covers the branch.
- (Resolved, #536) Every snapshot used to be stored as one row with an
  empty `snapshot_id`: rustic 0.11 groups `snapshots --json` output by
  host/label/paths (`[{"group_key": …, "snapshots": […]}]`) and
  `parse_snapshots_output` read each group as a snapshot, so
  `restore exec --snapshot latest` resolved to `''`. The parser now
  flattens group objects (and the older `[group_key, [snapshots]]` pairs),
  skips id-less entries with a warning, and `scan` deletes the `''` rows an
  earlier scan left (`delete_blank_snapshots`, `src/lcsas/db/snapshots.py`).
  Found by `tests/integration/test_scan_real_repo.py` (#517), the first
  test to run `cmd_scan`'s snapshot block against a real rustic repo.

---

*Document generated as part of the LCSAS workflow matrix — see
`docs/workflows/` for sibling docs covering bin-packing, staging, ISO
mastering, ECC, burning, restoration, consolidation, and meta-volume
workflows. For the system-level picture (tier model, holographic catalog,
schema v10), see [`docs/architecture.md`](../architecture.md); for the Rustic
on-disk pack format the scanner walks, see
[`docs/RESTIC_FORMAT_SPEC.md`](../RESTIC_FORMAT_SPEC.md).*
