# Workflows: Init & Config

First-time setup and ongoing config management: create the SQLite catalog and validate a TOML config. These run before any scan/stage/burn cycle.

Schema version is 10 (`CURRENT_SCHEMA_VERSION` in `src/lcsas/db/schema.py`); the TOML loader resolves relative paths against the config file's parent dir (`src/lcsas/config/settings.py`).

## Table of contents

- [`lcsas init`](#lcsas-init) — initialize the SQLite catalog
- [`lcsas config check`](#lcsas-config-check) — validate the TOML config
- [Notes & gaps](#notes--gaps) — observations from reading the source

---

## `lcsas init`

**Purpose:** Create an empty SQLite catalog and stamp it with the current schema version.

**Prerequisites:**
- A writable parent directory for the chosen DB path; missing intermediate dirs are fine — the handler calls `mkdir(parents=True, exist_ok=True)`.
- Optional TOML config — when `--config` is set, `paths.database` from the config is used as the DB location.

**Steps:**
1. `lcsas [--config FILE] init [--db-path PATH]` — create the SQLite file and run `ensure_schema()` (which calls `create_all()` on a fresh DB). (`cmd_init`, `src/lcsas/cli/main.py`)
   - Parser: `init` subparser in `build_parser()` (`src/lcsas/cli/main.py`).
   - DB path resolution order: explicit `--db-path` > global `--db` > `--config`'s `paths.database` > `archive.db` in cwd.
2. `create_all()` issues `CREATE TABLE IF NOT EXISTS` for every table and inserts a row into `schema_version` if empty. (`src/lcsas/db/schema.py`)

**Expected outcome:**
- A valid SQLite file exists at `--db-path` with tables `schema_version`, `volumes`, `repositories`, `packs`, `volume_packs`, `snapshots`, `locations`, `volume_copies`, `burn_sessions`, `session_volumes`, `volume_events`, `key_escrow`.
- `SELECT MAX(version) FROM schema_version` returns `CURRENT_SCHEMA_VERSION` (currently `10`).
- Idempotent — re-running against an existing DB is a no-op and returns 0.

**Variant axes that apply:**
- Media type: N/A.
- Multi-tenant: N/A — repos are registered later via `lcsas repo add`.
- OS: Linux/macOS expected to behave identically; untested on Windows (XDG paths in defaults — `_xdg_db_path` in `src/lcsas/config/settings.py`).
- Multi-copy: N/A.
- ECC: N/A.
- Recovery tier: Tier 0 (catalog only).

**Test coverage:**
- Existing:
  - `tests/unit/test_cli.py::TestCLIInit::test_init_creates_db` — DB file created.
  - `tests/unit/test_cli.py::TestCLIInit::test_init_honors_config_flag` — `--config` is honored (regression test for issue #17).
  - `tests/unit/test_cli.py::TestCLIParsing::test_init_command` — argparse wiring.
  - `tests/unit/test_cli_comprehensive.py::TestCmdInit::test_reinit_on_existing_db` — idempotent re-init.
  - `tests/unit/test_cli_comprehensive.py::TestCmdInit::test_init_creates_nested_parent_dirs` — the `mkdir(parents=True)` branch (`--db-path` under several missing directories, issue #40).
  - `tests/unit/test_db_schema.py::TestSchema::test_schema_version_recorded` — `create_all()` stamps `CURRENT_SCHEMA_VERSION`; `TestMigrateV3ToV4::test_migrate_updates_version` and `tests/unit/test_cli_repo_retirement.py::TestRepoRetireActivate::test_retire_migrates_a_v9_catalog_in_place` cover `ensure_schema()` migrating an older catalog.
- Gaps:
  - No CLI-level assertion that `init` itself leaves `schema_version == CURRENT_SCHEMA_VERSION` (intentional: `cmd_init` is a thin wrapper over `ensure_schema()`, and the stamp and migration are pinned one layer down by the tests above).

**Source refs:**
- Parser: `init` subparser in `build_parser()` (`src/lcsas/cli/main.py`)
- Handler: `cmd_init` (`src/lcsas/cli/main.py`)
- Schema DDL + `create_all` / `ensure_schema`: `src/lcsas/db/schema.py`
- Schema version constant: `CURRENT_SCHEMA_VERSION` (`src/lcsas/db/schema.py`)
- Catalog overview: `docs/architecture.md`

---

## `lcsas config check`

**Purpose:** Load a TOML config and report every validation error in a single pass.

**Prerequisites:**
- A TOML file at the path passed via the global `--config` flag (lives on the top-level parser, not the `config check` subparser — `--config` in `build_parser()`, `src/lcsas/cli/main.py`).
- Paths referenced in the TOML must exist and be the correct type for a clean run.

**Steps:**
1. `lcsas --config PATH config check` — load and validate. (`cmd_config_check`, `src/lcsas/cli/main.py`)
   - Parser: `config` subparser in `build_parser()` (`src/lcsas/cli/main.py`).
   - Missing `--config` logs `--config is required for config check.` and returns 1.
2. `load_config()` parses via `tomllib`, warns on unknown sections/keys, resolves relative paths, and builds a frozen `LCSASConfig`. (`src/lcsas/config/settings.py`)
3. `validate_config()` checks (`src/lcsas/config/settings.py`):
   - `mirror_base_path` exists and is a directory.
   - `staging_path` exists, is a directory, and is writable.
   - `db_path` parent exists and is writable.
   - `default_ecc_redundancy_pct` in `[0, 100]`.
   - `metadata_reserve_bytes` non-negative and `< default_media_type.usable_bytes`.
   - `label_prefix` non-empty, matches `[A-Z0-9_]+`, short enough for a 32-char ISO 9660 label.
   - Per-repo `mirror_path` exists and is a directory; `password_file` exists if set.
   - `staging_path` and `mirror_base_path` are not identical or nested (cleanup would destroy mirrors).

**Expected outcome:**
- Valid: one `Configuration is valid.` log line, exit 0.
- Invalid: one log line per error, exit 1. All errors reported in one pass.

**Variant axes that apply:**
- Media type: `defaults.media_type` gates `metadata_reserve_bytes` against `usable_bytes`; test media (`TEST_TINY`) accepted (`MediaType` in `src/lcsas/config/media.py`).
- Multi-tenant: each `[repos.<name>]` block validated independently; one error per failing repo.
- OS: filesystem semantics of `Path.resolve()` and `os.access(..., W_OK)` matter; read-only mounts trip `staging_path is not writable`.
- Multi-copy: N/A.
- ECC: `default_ecc_redundancy_pct` range-checked; deprecated (BURN-07) —
  it has no effect on RS03 augmented images (dvdisaster pads to the
  smallest fitting medium), so any non-default value logs a WARNING.
- Recovery tier: Tier 0.

**Test coverage:**
- Existing:
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_valid_config` — happy path.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_missing_paths_errors` — missing dirs reported.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_config_required` — `--config` omitted returns 1.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_bad_ecc_redundancy` — out-of-range ECC.
  - `tests/unit/test_config_validation.py::*` — mirror/staging missing/file, db parent missing, ECC range, negative metadata reserve, per-repo paths, password file.
  - `tests/unit/test_config.py::TestTomlKeyValidation` — the loader's unknown-section and unknown-`[defaults]`-key warnings.
- Gaps:
  - No test at any level (not just the CLI) for three `validate_config()` branches: `staging_path` overlapping or equal to `mirror_base_path`, `label_prefix` validation (empty, non-`[A-Z0-9_]`, too long for a 32-char ISO 9660 label), and `metadata_reserve_bytes >= usable_bytes`.
  - Unknown-key warnings are covered only at the loader level and only for unknown sections and `[defaults]` keys (not `[paths]`, `[survivability]` or `[repos.<name>]` keys), and not via `config check`. Note that `config check` prints `Configuration is valid.` and exits 0 even when `load_config()` warned about an unknown section or key (see the last bullet under Notes & gaps).
  - `optical_device` is not validated (intentional: `config check` runs on hosts that need not have the burner attached, and `load_config()` deliberately does no I/O (#427); the device is checked at burn time by `cmd_burn_session`, which refuses a missing node, and `BurnOrchestrator._assert_blank_media`).

**Source refs:**
- Parser / dispatch / handler: `config` subparser + `cmd_config_check` (`src/lcsas/cli/main.py`).
- Loader / validator / default-config factory: `load_config` / `validate_config` (`src/lcsas/config/settings.py`).
- Unknown-key warning whitelist: `_validate_toml_keys` in `src/lcsas/config/settings.py`.

---

## Notes & gaps

Observations from reading the source; **not** fixes.

- **Catalog distribution is holographic.** The complete SQLite catalog is copied onto every burned disc by `staging/metadata.py::HolographicInjector`, so any single disc is self-describing. There is no separate JSON-export step. (intentional: design note, not a defect.)
- **True backup = copy the `.sqlite` file.** There is no `db import` and no `db export` command; operators wanting an off-disc snapshot of the catalog should copy the raw SQLite file. The catalog runs in WAL mode (`get_connection` in `src/lcsas/db/connection.py`), so copy it while no `lcsas` command is running (a copy taken mid-command can miss the `-wal` sidecar), or take a consistent copy with `sqlite3 archive.db ".backup copy.db"`. (intentional: the holographic copy on every disc is the designed backup; no export command is planned.)
- **`init` honors `--config`.** `lcsas --config foo.toml init` writes to the TOML's `paths.database` (resolution order: `--db-path` > `--db` > `--config` > `./archive.db`) — fixed in issue #17 and pinned by `tests/unit/test_cli.py::TestCLIInit::test_init_honors_config_flag` (`cmd_init`, `src/lcsas/cli/main.py`). Resolved; kept as a behaviour note.
- **`init` migrates if needed.** On an existing catalog `ensure_schema()` calls `migrate()` to bring an older DB up to `CURRENT_SCHEMA_VERSION`; on a fresh DB `create_all()` stamps the current version directly (`src/lcsas/db/schema.py`). Pinned by `tests/unit/test_db_schema.py` (`TestSchema`, `TestMigrateV3ToV4`). Resolved; kept as a behaviour note.
- **`config check` does not validate `optical_device`** — typos surface only at burn time. (intentional: see the Gaps bullet under `lcsas config check`.)
- **`--config` is a top-level flag.** `lcsas config check --config foo.toml` fails argparse (`lcsas: error: unrecognized arguments: --config foo.toml`); correct form is `lcsas --config foo.toml config check`. The error message does not say that the flag belongs before the subcommand. (Still open: usability only.)
- **Unknown TOML keys are warnings, not errors.** `_validate_toml_keys` (`src/lcsas/config/settings.py`) only logs `Unknown ... (typo?)`; `config check` still prints `Configuration is valid.` and exits 0, and `scan` exits 0. Reproduced 2026-10-21: `[repo.family]` (singular, so zero repos load) gives `Total scanned: 0 packs across 0 repos`, rc 0; `passwrd_file` under `[repos.family]` is silently ignored (the repo is scanned with no password file, so snapshot listing is skipped); a typo'd `mirror_path` is worse than a warning, a bare `Unexpected error: 'mirror_path'` (the unhandled `KeyError` in `load_config()`, the same class as #429). (Still open.)
