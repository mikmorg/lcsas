# Workflows: Init & Config

First-time setup and ongoing config management: create the SQLite catalog and validate a TOML config. These run before any scan/stage/burn cycle.

Schema version is 9 (`CURRENT_SCHEMA_VERSION` in `src/lcsas/db/schema.py`); the TOML loader resolves relative paths against the config file's parent dir (`src/lcsas/config/settings.py`).

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
- `SELECT MAX(version) FROM schema_version` returns `9`.
- Idempotent — re-running against an existing DB is a no-op and returns 0.

**Variant axes that apply:**
- Media type: N/A.
- Multi-tenant: N/A — repos are registered later via `lcsas repo add`.
- OS: Linux/macOS expected to behave identically; untested on Windows (XDG paths in defaults — `src/lcsas/config/settings.py:66`).
- Multi-copy: N/A.
- ECC: N/A.
- Recovery tier: Tier 0 (catalog only).

**Test coverage:**
- Existing:
  - `tests/unit/test_cli.py::TestCLIInit::test_init_creates_db` — DB file created.
  - `tests/unit/test_cli.py::TestCLIInit::test_init_honors_config_flag` — `--config` is honored (regression test for issue #17).
  - `tests/unit/test_cli.py::TestCLIParsing::test_init_command` — argparse wiring.
  - `tests/unit/test_cli_comprehensive.py::TestCmdInit::test_reinit_on_existing_db` — idempotent re-init.
- Gaps:
  - No assertion that `schema_version` actually equals 9 after `init`.
  - No coverage for the `mkdir(parents=True)` branch (e.g., `--db-path /tmp/new/dir/archive.db`).

**Source refs:**
- Parser: `init` subparser in `build_parser()` (`src/lcsas/cli/main.py`)
- Handler: `cmd_init` (`src/lcsas/cli/main.py`)
- Schema DDL + `create_all` / `ensure_schema`: `src/lcsas/db/schema.py`
- Schema version constant: `CURRENT_SCHEMA_VERSION` (`src/lcsas/db/schema.py`)
- Catalog overview: `docs/architecture/overview.md`

---

## `lcsas config check`

**Purpose:** Load a TOML config and report every validation error in a single pass.

**Prerequisites:**
- A TOML file at the path passed via the global `--config` flag (lives on the top-level parser, not the `config check` subparser — `src/lcsas/cli/main.py:55`).
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
- Media type: `defaults.media_type` gates `metadata_reserve_bytes` against `usable_bytes`; test media (`TEST_TINY`) accepted (`src/lcsas/config/media.py:26`).
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
  - `tests/unit/test_config_validation.py::*` — every `validate_config()` branch (mirror/staging missing/file, db parent missing, ECC range, metadata reserve, per-repo paths, password file).
- Gaps:
  - No CLI-level test for staging-overlaps-mirror, `label_prefix` validation, or `metadata_reserve_bytes >= usable_bytes`.
  - Unknown-section/unknown-key warnings (`src/lcsas/config/settings.py:78`) covered only at the loader level, not via `config check`.
  - `optical_device` not validated (gap, not a test gap).

**Source refs:**
- Parser / dispatch / handler: `config` subparser + `cmd_config_check` (`src/lcsas/cli/main.py`).
- Loader / validator / default-config factory: `load_config` / `validate_config` (`src/lcsas/config/settings.py`).
- Unknown-key warning whitelist: `src/lcsas/config/settings.py`.

---

## Notes & gaps

Observations from reading the source; **not** fixes.

- **Catalog distribution is holographic.** The complete SQLite catalog is copied onto every burned disc by `staging/metadata.py::HolographicInjector`, so any single disc is self-describing. There is no separate JSON-export step.
- **True backup = copy the `.sqlite` file.** There is no `db import` and no `db export` command; operators wanting an off-disc snapshot of the catalog should copy the raw SQLite file.
- **`init` honors `--config`.** `lcsas --config foo.toml init` writes to the TOML's `paths.database` (resolution order: `--db-path` > `--db` > `--config` > `./archive.db`) — fixed in issue #17 (`cmd_init`, `src/lcsas/cli/main.py`).
- **`init` migrates if needed.** On an existing catalog `ensure_schema()` calls `migrate()` to bring an older DB up to `CURRENT_SCHEMA_VERSION`; on a fresh DB `create_all()` stamps the current version directly (`src/lcsas/db/schema.py`).
- **`config check` does not validate `optical_device`** — typos surface only at burn time.
- **`--config` is a top-level flag.** `lcsas config check --config foo.toml` fails argparse; correct form is `lcsas --config foo.toml config check`. The error message could be clearer about position.
- **Unknown TOML keys are warnings, not errors.** A typo-quiet config can load "successfully" and silently produce nothing on `scan` (`src/lcsas/config/settings.py:78`).
