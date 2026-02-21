# Plan: Phase 5 — Gaps, CLI Tests, Features & Hardening

Fix the `config.device` bug (P0), then implement 11 features in dependency order: foundational infrastructure first (logging, file locking, config validation, TMPDIR, bulk_register), then functional features (SHA-256 ingest verification, --dry-run, orphaned staging cleanup, snapshot persistence), then security hardening (input sanitization, signal handlers, password path masking), and finally comprehensive CLI handler tests for all 17 handlers.

## Step 1 — P0 Bug Fix + Trivial Wins (minutes)

1. **Fix `config.device` bug:** In `src/lcsas/cli/main.py` L525, change `config.device` → `config.optical_device`.
2. **Use `bulk_register` in DeltaAnalyzer (Feature #6):** Refactor `register_new_packs()` in `src/lcsas/packs/delta.py` L37 to collect all `(sha256, size_bytes, repo_id)` tuples from `self._scanner_result`, filter out those already in DB with a single `SELECT ... WHERE sha256 IN (...)` query, then call `bulk_register()` from `src/lcsas/db/packs.py` L74 once. Reduces 2N queries → 2 queries.
3. **Add unit test** for `register_new_packs()` verifying it calls `bulk_register` (or at minimum produces same results as before).

## Step 2 — Logging Framework (Feature #1)

1. **Create `src/lcsas/log.py`** with a `setup_logging(verbose: bool = False)` function:
   - Configure stdlib `logging` with a named logger `"lcsas"`
   - Default level `INFO`, `--verbose` switches to `DEBUG`
   - Format: `%(levelname)s: %(message)s` (no timestamps for CLI, keeps output clean)
   - Return the configured logger

2. **Replace all 101 `print()` calls** in `src/lcsas/cli/main.py` with appropriate log calls:
   - User-facing output (status, tables, JSON) → `logger.info()`
   - Operational details (paths, counts) → `logger.debug()`
   - Errors → `logger.error()`
   - Success confirmations → `logger.info()`

3. **Wire `setup_logging()`** in `main()` at L1040, right after argument parsing, using `args.verbose`.

4. **Add loggers to non-CLI modules** that would benefit (burn orchestrator, restore executor, staging builder) — import `logging.getLogger(__name__)` and add `logger.debug()` calls at key operation boundaries. These modules currently have zero output; adding debug-level logging gives visibility without changing behavior.

5. **Update tests:** Existing CLI tests capture output via `capsys`. After migration to logging, either:
   - Use `caplog` fixture (pytest built-in) to capture log records, OR
   - Attach a `StreamHandler` to `"lcsas"` logger pointing at stdout so `capsys` still works
   - Recommend `caplog` as it's cleaner and allows level-based assertions.

## Step 3 — Infrastructure Hardening (Features #10, #11, #12)

### 3a. File Locking (Feature #11)

1. In `src/lcsas/db/connection.py`, add `PRAGMA busy_timeout=30000` (30 seconds) to `get_connection()`, after WAL mode is enabled.
2. Add an optional `exclusive: bool = False` parameter. When `True`, acquire an `fcntl.flock(LOCK_EX)` on a `<db_path>.lock` lockfile before returning the connection.
3. Create a context manager `locked_connection(db_path)` that acquires the lock, yields the connection, and releases on exit (including on exception).
4. Update `cmd_stage`, `cmd_burn_session`, `cmd_burn_legacy`, and `cmd_scan` to use `locked_connection()`.
5. Add tests: concurrent access with `busy_timeout`, lock acquisition/release.

### 3b. TMPDIR Passthrough (Feature #10)

1. Add a `tmpdir: Path | None = None` parameter to the subprocess runner protocol types (`XorrisoRunner`, `DVDisasterRunner`, `RusticRunner`) and their subprocess implementations.
2. In each `subprocess.run()` call in `src/lcsas/iso/xorriso.py`, `src/lcsas/ecc/dvdisaster.py`, and `src/lcsas/rustic/wrapper.py`, pass `env={**os.environ, "TMPDIR": str(tmpdir)}` when `tmpdir` is not None.
3. In CLI handlers that create runners, pass `config.staging_path` (or a dedicated `config.tmp_path` if we add one) as the tmpdir. Staging path is already guaranteed to be a large working area.
4. Also update the `tempfile.mkdtemp()` call in `cmd_restore_exec` at L934 to use `dir=config.staging_path`.
5. Update protocol stubs in test mocks to accept the new parameter.

### 3c. Config Validation (Feature #12)

1. Add a `validate_config(config: LCSASConfig) -> list[str]` function to `src/lcsas/config/settings.py`:
   - Check `mirror_path` exists and is a directory
   - Check `staging_path` exists, is a directory, and is writable
   - Check `db_path` parent directory exists and is writable
   - For each repo: check `password_file` path exists if specified
   - Check `ecc_redundancy_pct` is in range 0-100
   - Check `metadata_reserve_mb` is a positive number
   - Return list of warning/error strings (empty = valid)
2. Add `cmd_config_check` handler to `src/lcsas/cli/main.py`: load config, call `validate_config()`, print results, return 0 if clean or 1 if errors.
3. Wire `config check` subcommand in `build_parser()` and `dispatch()`.
4. Optionally: call `validate_config()` at the start of `cmd_stage` and `cmd_burn_*` (the high-risk commands) and abort if critical errors exist.
5. Add tests for each validation case.

## Step 4 — Functional Features (Features #3, #4, #5)

### 4a. SHA-256 Verification on Restore Ingest (Feature #3)

1. In `src/lcsas/restore/executor.py` L84, after `copy_file(src, dst)`, add:
   - Call `sha256_file(dst)` from `src/lcsas/utils/hashing.py`
   - Compare result to expected `sha256` (the pack filename)
   - On mismatch: `dst.unlink()`, log error, raise `IntegrityError` (or a new `PackCorruptionError`)
   - Log at debug level: `"Verified pack {sha256} ({size} bytes)"`
2. Add `--skip-verify` flag to `cmd_restore_exec` for users who want speed over safety (off by default — always verify).
3. Add unit tests: good hash passes, bad hash raises and deletes file, `--skip-verify` skips check.

### 4b. `--dry-run` Mode (Feature #4)

1. Add `--dry-run` / `-n` flag to `stage`, `burn`, and `burn-legacy` subcommands in `build_parser()`.
2. In `BurnOrchestrator`, add `dry_run: bool = False` parameter to `stage()`:
   - Run `_gather_packs_for_staging()` and `_multi_bin_pack()` as normal (compute only)
   - If `dry_run`: log the plan (volume count, packs per volume, total bytes, estimated disc fill %), then return early without creating session, staging dirs, ISOs, or DB rows
3. In `cmd_burn_session`, if `dry_run`: list the session's volumes and their status, but don't call `burn_session()`.
4. Pass `args.dry_run` through from CLI handlers.
5. Add tests verifying dry-run produces output but makes no DB/FS changes.

### 4c. Orphaned Staging Cleanup (Feature #5)

1. Add `detect_orphaned_staging(config: LCSASConfig, conn: Connection) -> list[Path]` to a new `src/lcsas/staging/cleanup.py`:
   - List all directories under `config.staging_path`
   - Query `burn_sessions` for all known `staging_dir` values where `status != 'CLEANED'`
   - Return directories that exist on disk but don't match any active session
2. Add `clean_orphaned_staging(paths: list[Path]) -> int` that calls `safe_remove_tree()` on each, returns count.
3. Add `cmd_staging_clean` handler: detect orphans, print them, prompt for confirmation (or `--force` to skip prompt), clean, log results.
4. Wire `staging clean` subcommand.
5. Add tests with mock filesystem.

## Step 5 — Snapshot Persistence (Feature #7)

1. **Add DB functions** in a new `src/lcsas/db/snapshots.py`:
   - `upsert_snapshot(conn, snapshot_id, repo_id, hostname, timestamp, paths, tags, description)` — INSERT OR REPLACE
   - `bulk_upsert_snapshots(conn, snapshots: list[Snapshot])` — executemany
   - `list_snapshots(conn, repo_id: str | None = None) -> list[Snapshot]`
   - `get_snapshot(conn, snapshot_id: str) -> Snapshot | None`

2. **Integrate with `cmd_scan`:** After scanning packs, also run `rustic snapshots` (already parsed by `rustic/parser.py`), convert `SnapshotInfo` → `Snapshot` model, call `bulk_upsert_snapshots()`. This makes `scan` the single source-of-truth command.

3. **Add `--snapshots` flag** to `cmd_scan` (on by default, `--no-snapshots` to skip) since snapshot listing requires a rustic call that may be slow.

4. **Add `snapshots` subcommand** or integrate into `status` output: list persisted snapshots from DB, grouped by repo.

5. **Add tests:** Upsert, bulk upsert, list, integration with scan flow.

## Step 6 — Security & Robustness Hardening

### 6a. Input Sanitization

1. Add `sanitize_name(value: str, field: str) -> str` to `src/lcsas/utils/labels.py`:
   - Reject or strip path separators (`/`, `\`, `..`)
   - Reject null bytes
   - Enforce max length (e.g., 128 chars)
   - Raise `ValueError` with descriptive message
2. Apply to user-provided inputs: location names in `cmd_location`, label arguments in `cmd_verify`, any free-text input that ends up in filenames.
3. Add tests for each rejection case.

### 6b. Signal Handlers / Graceful Shutdown

1. Add `src/lcsas/utils/shutdown.py` with:
   - `ShutdownManager` class: registers cleanup callbacks, installs `SIGTERM`/`SIGINT` handlers
   - On signal: sets a `_shutting_down` flag, runs registered cleanups in reverse order, then re-raises
2. In `cmd_stage`: register `orchestrator.abort()` as cleanup callback before calling `stage()`.
3. In `cmd_burn_session`: register a callback that updates volume status to `FAILED` if interrupted mid-burn.
4. Keep it simple — no threading complexity. Just `signal.signal(SIGTERM, handler)` at the start of long-running handlers.
5. Add tests: verify cleanup callbacks run on simulated signal.

### 6c. Temp File Cleanup

1. In `cmd_restore_exec`, wrap the `tempfile.mkdtemp()` usage in a `try/finally` that calls `safe_remove_tree()` on the cache dir. (Some of this may already exist from Phase 4 — verify and fill gaps.)
2. Register the temp dir with `ShutdownManager` as well, so it's cleaned on SIGTERM.

### 6d. Password Path Masking

1. In error messages and log output, if a path ends with common password file suffixes or matches a known password-file config value, mask it. Lightweight: only apply in `logger.error()` paths, not everywhere.
2. This is low priority — current code doesn't print password paths, but it's defensive for future logging additions.

## Step 7 — CLI Handler Tests (All 17 Handlers)

Test architecture: Create mock-based test patterns since most handlers require config, external tools, and filesystem.

1. **Add a `cli_test_helpers` fixture module** in `tests/fixtures/`:
   - `mock_config(tmp_path)` → returns `LCSASConfig` with `TEST_TINY` media, tmp_path-based paths, real SQLite in tmp_path
   - `mock_args(**overrides)` → returns `Namespace` with common defaults
   - `mock_rustic_runner()` → returns a mock `RusticRunner` that returns canned JSON
   - `mock_xorriso_runner()` → returns a mock `XorrisoRunner` (safe no-op)
   - `mock_dvdisaster_runner()` → returns a mock `DVDisasterRunner`

2. **Handlers already tested** (verify coverage, add edge cases):
   - `cmd_init` — add: re-init on existing DB, invalid path
   - `cmd_repo_add` — add: duplicate repo name, missing required args
   - `cmd_repo_list` — add: many repos, empty DB
   - `cmd_status` — add: with volumes in various states
   - `cmd_db_export` — add: verify JSON schema completeness

3. **Handlers needing new tests** (11 handlers):
   - `cmd_scan` — mock `scan_mirror_packs()` and `DeltaAnalyzer`; verify packs registered, output shows counts
   - `cmd_stage` — mock `BurnOrchestrator.stage()`; verify session created, dry-run output, error handling
   - `cmd_burn_session` — mock `BurnOrchestrator.burn_session()`; verify status flow, dry-run, missing session error
   - `cmd_burn_legacy` — mock orchestrator; verify `config.optical_device` is used (catches P0 regression), end-to-end flow
   - `cmd_burn_iso` — mock `SubprocessXorrisoRunner`; verify burn + verify calls
   - `cmd_location` (3 sub-handlers) — real DB, test add/list/move with edge cases (duplicate, nonexistent volume)
   - `cmd_catalog_import` — create fake receipt JSON files in tmp_path, verify DB inserts
   - `cmd_consolidate` — mock `VolumeMerger.plan_consolidation()`; verify output formatting
   - `cmd_verify` — mock xorriso/dvdisaster runners; verify disc check flow
   - `cmd_meta_build` — mock `MetaVolumeBuilder`; verify build() is called with correct args
   - `cmd_restore_plan` — mock `RusticRunner.restore_dry_run()` and `RestorePlanner`; verify pick list output
   - `cmd_restore_exec` — mock `RestoreExecutor`; verify ingest + execute flow, cache cleanup

4. **New subcommand tests** (added in this phase):
   - `cmd_config_check` — test with valid config, missing paths, bad values
   - `cmd_staging_clean` — mock filesystem, test detection and cleanup

5. **Target:** 100% handler coverage with at least happy-path + one error-path test per handler. Estimated ~40-50 new test cases.

## Verification

- `make test` (or `pytest tests/`) — all existing 486 tests + new tests pass
- `pytest --cov=src/lcsas/cli --cov-report=term-missing` — verify all 17+ handlers have coverage
- `ruff check src/ tests/` — no lint errors
- `mypy src/` — no type errors
- Manual smoke test: `lcsas config check` with a real config file
- Manual smoke test: `lcsas stage --dry-run` with a real repo

## Decisions

- **Logging format:** `%(levelname)s: %(message)s` — no timestamps (CLI tool, not daemon). Users wanting timestamps can configure via `LCSAS_LOG_FORMAT` env var later.
- **File locking approach:** `fcntl.flock()` + `PRAGMA busy_timeout` — belt and suspenders. Exclusive lock only for write-heavy commands, not reads.
- **TMPDIR target:** Use `config.staging_path` as the default TMPDIR for subprocesses, since it's already a user-configured large working area.
- **Dry-run scope:** Stage and burn only. Read-only commands (status, restore-plan) don't need it. `scan` is idempotent (INSERT OR IGNORE) so dry-run adds little value.
- **Snapshot persistence trigger:** Integrated into `cmd_scan`, not a separate command. Scan is already the "sync local state with mirror" operation.
- **Test pattern:** Mock-based for CLI handlers (via `unittest.mock.patch`), not integration tests. This avoids requiring real rustic/xorriso/dvdisaster binaries for unit tests.
- **Feature #2 (schema migration) is skipped** per user direction — but Step 5 (snapshots) doesn't need new schema since the `snapshots` table already exists in schema v2.
- **Feature #8 (catalog rebuild from disc) deferred** — catalog IS the SQLite DB; current `catalog import-receipts` only records volume locations, not a full rebuild. That's a Phase 8 effort.
