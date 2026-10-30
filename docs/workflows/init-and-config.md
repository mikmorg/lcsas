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
- A writable parent directory for the chosen catalog path; missing intermediate dirs are fine — the handler calls `mkdir(parents=True, exist_ok=True)`.
- Optional TOML config — when `--config` is set, `paths.database` from the config is used as the catalog path.

**Steps:**
1. `lcsas [--config FILE] init [--db-path PATH]` — create the SQLite file and run `ensure_schema()` (which calls `create_all()` on a fresh catalog). (`cmd_init`, `src/lcsas/cli/main.py`)
   - Parser: `init` subparser in `build_parser()` (`src/lcsas/cli/main.py`).
   - Catalog path resolution order: explicit `--db-path` > global `--db` > `--config`'s `paths.database` > `archive.db` in cwd.
2. `create_all()` issues `CREATE TABLE IF NOT EXISTS` for every table and inserts a row into `schema_version` if empty. (`src/lcsas/db/schema.py`)

**Expected outcome:**
- A valid SQLite file exists at `--db-path` with tables `schema_version`, `volumes`, `repositories`, `packs`, `volume_packs`, `snapshots`, `locations`, `volume_copies`, `burn_sessions`, `session_volumes`, `volume_events`, `key_escrow`.
- `SELECT MAX(version) FROM schema_version` returns `CURRENT_SCHEMA_VERSION` (currently `10`).
- Idempotent — re-running against an existing catalog is a no-op and returns 0.

**Variant axes that apply:**
- Media type: N/A.
- Multi-repository: N/A — repositories are registered later via `lcsas repo add`.
- OS: Linux/macOS expected to behave identically; untested on Windows (XDG paths in defaults — `_xdg_db_path` in `src/lcsas/config/settings.py`).
- Multi-copy: N/A.
- ECC: N/A.
- Storage stage: none (master catalog only).

**Test coverage:**
- Existing:
  - `tests/unit/test_cli.py::TestCLIInit::test_init_creates_db` — catalog file created.
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
   - `label_prefix` non-empty, matches `[A-Z0-9_]+`, short enough for a 32-char ISO 9660 label with the configured `media_type`'s token (CD700, BDXL100 and MDISC100 allow one character less than BD25); a prefix that fits the configured media type but not another production media type logs a WARNING, since `stage`/`burn --media` can pick it.
   - Per-repository `mirror_path` exists and is a directory; `password_file` exists if set.
   - `staging_path` and `mirror_base_path` are not identical or nested (cleanup would destroy mirrors).

**Expected outcome:**
- Valid: one `Configuration is valid.` log line, exit 0.
- Invalid: one log line per error, exit 1. All errors reported in one pass.

**Variant axes that apply:**
- Media type: `defaults.media_type` gates `metadata_reserve_bytes` against `usable_bytes`; the test media type (`TEST_TINY`) is accepted (`MediaType` in `src/lcsas/config/media.py`).
- Multi-repository: each `[repos.<name>]` block validated independently; one error per failing repository.
- OS: filesystem semantics of `Path.resolve()` and `os.access(..., W_OK)` matter; read-only mounts trip `staging_path is not writable`.
- Multi-copy: N/A.
- ECC: `default_ecc_redundancy_pct` range-checked; deprecated (BURN-07) —
  it has no effect on RS03 augmented images (dvdisaster pads to the
  smallest fitting medium), so any non-default value logs a WARNING.
- Storage stage: none (config only).

**Test coverage:**
- Existing:
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_valid_config` — happy path.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_missing_paths_errors` — missing dirs reported.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_config_required` — `--config` omitted returns 1.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_bad_ecc_redundancy` — out-of-range ECC.
  - `tests/unit/test_config_validation.py::*` — mirror/staging missing/file, catalog parent dir missing, ECC range, negative metadata reserve, per-repository paths, password file.
  - `tests/unit/test_config.py::TestTomlKeyValidation` — the loader's unknown-section and unknown-`[defaults]`-key warnings.
  - `tests/unit/test_config_validate_gaps.py::TestStagingMirrorOverlap` — `staging_path` identical to `mirror_base_path`, nested inside it or containing it, or a symlinked alias of it, is rejected; a sibling that merely shares a name prefix (`mirror` vs `mirror2`) is accepted.
  - `tests/unit/test_config_validate_gaps.py::TestLabelPrefix` — empty prefix and non-`[A-Z0-9_]` characters rejected; for BD25 a 17-character prefix is accepted (and yields a 32-character label from `generate_volume_label`) and an 18-character one is reported as too long; a bad-charset prefix reports only the charset error; the ceiling follows the configured media type (`::test_17_char_prefix_rejected_for_five_char_media_token`, and `::test_any_accepted_prefix_yields_legal_labels_for_configured_media` over every production media type), and a prefix too long only for another media type warns instead of failing (`::test_prefix_too_long_only_for_other_media_warns_not_errors`) (#523).
  - `tests/unit/test_config_validate_gaps.py::TestMetadataReserveVsUsable` — `metadata_reserve_bytes` equal to or above the configured media type's `usable_bytes` rejected (message names the media type), one byte below accepted, the limit follows `default_media_type` (BD25 vs CD700), and a negative value takes the "must not be negative" branch instead.
  - `tests/unit/test_config_validate_gaps.py::TestUnknownKeyWarnings` — the loader warns (and still loads) for unknown `[paths]`, `[survivability]` and `[repos.<name>]` keys: each warning names the table and every unknown key, repositories are checked independently, known keys never warn, a typo'd key's value is silently dropped, and the warnings do not make `validate_config()` fail.
  - `tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_section_typo_fails_the_check` and `::test_repo_key_typo_fails_the_check` — `config check` itself exits 1 on an unknown section and on an unknown `[repos.<name>]` key (#515).
- Gaps:
  - No `config check`-level test for an unknown `[paths]`, `[defaults]` or `[survivability]` key (only the loader-level warnings above, and the section-typo and `[repos.<name>]`-key CLI tests, exist).
  - `optical_device` is not validated (intentional: `config check` runs on hosts that need not have the burner attached, and `load_config()` deliberately does no I/O (#427); the device is checked at burn time by `cmd_burn_session`, which refuses a missing node, and `BurnOrchestrator._assert_blank_media`).

**Source refs:**
- Parser / dispatch / handler: `config` subparser + `cmd_config_check` (`src/lcsas/cli/main.py`).
- Loader / validator / default-config factory: `load_config` / `validate_config` (`src/lcsas/config/settings.py`).
- Unknown-key warning whitelist: `_validate_toml_keys` in `src/lcsas/config/settings.py`.

---

## Notes & gaps

Observations from reading the source; **not** fixes.

- **Every disc carries a disc catalog.** The complete SQLite catalog is copied onto every burned disc by `staging/metadata.py::HolographicInjector`, so any single disc is self-describing. There is no separate JSON-export step. (intentional: design note, not a defect.)
- **True backup = copy the `.sqlite` file.** There is no `db import` and no `db export` command; an owner wanting an off-disc copy of the master catalog should copy the raw SQLite file. The catalog runs in WAL mode (`get_connection` in `src/lcsas/db/connection.py`), so copy it while no `lcsas` command is running (a copy taken mid-command can miss the `-wal` sidecar), or take a consistent copy with `sqlite3 archive.db ".backup copy.db"`. (intentional: the disc catalog on every disc is the designed backup; no export command is planned.)
- **`init` honors `--config`.** `lcsas --config foo.toml init` writes to the TOML's `paths.database` (resolution order: `--db-path` > `--db` > `--config` > `./archive.db`) — fixed in issue #17 and pinned by `tests/unit/test_cli.py::TestCLIInit::test_init_honors_config_flag` (`cmd_init`, `src/lcsas/cli/main.py`). Resolved; kept as a behaviour note.
- **`init` migrates if needed.** On an existing catalog `ensure_schema()` calls `migrate()` to bring an older catalog up to `CURRENT_SCHEMA_VERSION`; on a fresh catalog `create_all()` stamps the current version directly (`src/lcsas/db/schema.py`). Pinned by `tests/unit/test_db_schema.py` (`TestSchema`, `TestMigrateV3ToV4`). Resolved; kept as a behaviour note.
- **`config check` does not validate `optical_device`** — typos surface only at burn time. (intentional: see the Gaps bullet under `lcsas config check`.)
- **`--config` is a top-level flag.** `lcsas config check --config foo.toml` fails argparse (`lcsas: error: unrecognized arguments: --config foo.toml`); correct form is `lcsas --config foo.toml config check`. The error now adds `--config: top-level option(s); put them before the subcommand, ...` (the same for `--db`, `--lock-timeout` and `--verbose`); the exit is still argparse's status 2 with nothing run (`_LcsasArgumentParser` in `src/lcsas/cli/main.py`; `tests/unit/test_cli_config_placement.py`, #517).
- **Unknown TOML keys are warnings everywhere except `config check`.** (Resolved for `config check` by #515.) `unknown_toml_keys` (`src/lcsas/config/settings.py`) lists unknown sections and keys; `load_config()` only logs each as `Unknown ... (typo?)`, so no command is blocked by a typo, but `config check` now adds an error and exits 1 instead of printing `Configuration is valid.` (`tests/unit/test_cli_comprehensive.py::TestCmdConfigCheck::test_section_typo_fails_the_check` and `::test_repo_key_typo_fails_the_check`). A `[repos.<name>]` block without `mirror_path` is now reported by name (`::test_missing_mirror_path_is_named`) instead of a bare `Unexpected error: 'mirror_path'`. Other commands such as `scan` still only warn, so a typo'd `passwrd_file` is still dropped silently there (the repository is scanned with no password file, so snapshot listing is skipped).
