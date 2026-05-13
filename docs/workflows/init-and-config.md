# Workflows: Init & Config

First-time setup and ongoing config management: create the SQLite catalog, validate a TOML config, and dump the catalog as JSON. These run before any scan/stage/burn cycle.

Schema version is 5 (`src/lcsas/db/schema.py:7`); the TOML loader resolves relative paths against the config file's parent dir (`src/lcsas/config/settings.py:151`).

## Table of contents

- [`lcsas init`](#lcsas-init) — initialize the SQLite catalog
- [`lcsas config check`](#lcsas-config-check) — validate the TOML config
- [`lcsas db export`](#lcsas-db-export) — dump the catalog summary as JSON
- [Notes & gaps](#notes--gaps) — observations from reading the source

---

## `lcsas init`

**Purpose:** Create an empty SQLite catalog and stamp it with the current schema version.

**Prerequisites:**
- A writable parent directory for the chosen DB path; missing intermediate dirs are fine — the handler calls `mkdir(parents=True, exist_ok=True)`.
- Optional TOML config — when `--config` is set, `paths.database` from the config is used as the DB location.

**Steps:**
1. `lcsas [--config FILE] init [--db-path PATH]` — create the SQLite file and run `create_all()`. (`src/lcsas/cli/main.py:447`)
   - Parser: `src/lcsas/cli/main.py:71`.
   - DB path resolution order: explicit `--db-path` > global `--db` > `--config`'s `paths.database` > `archive.db` in cwd.
2. `create_all()` issues `CREATE TABLE IF NOT EXISTS` for every table and inserts a row into `schema_version` if empty. (`src/lcsas/db/schema.py:170`)

**Expected outcome:**
- A valid SQLite file exists at `--db-path` with tables `schema_version`, `volumes`, `repositories`, `packs`, `volume_packs`, `snapshots`, `locations`, `volume_copies`, `burn_sessions`, `session_volumes`, `volume_events`.
- `SELECT MAX(version) FROM schema_version` returns `5`.
- Idempotent — re-running against an existing DB is a no-op and returns 0.

**Variant axes that apply:**
- Media type: N/A.
- Multi-tenant: N/A — repos are registered later via `lcsas repo add`.
- OS: Linux/macOS expected to behave identically; untested on Windows (XDG paths in defaults — `src/lcsas/config/settings.py:66`).
- Optical drive count: N/A.
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
  - No assertion that `schema_version` actually equals 5 after `init`.
  - No coverage for the `mkdir(parents=True)` branch (e.g., `--db-path /tmp/new/dir/archive.db`).

**Source refs:**
- Parser: `src/lcsas/cli/main.py:71`
- Handler: `src/lcsas/cli/main.py:447`
- Schema DDL + `create_all`: `src/lcsas/db/schema.py:170`
- Schema version constant: `src/lcsas/db/schema.py:7`

---

## `lcsas config check`

**Purpose:** Load a TOML config and report every validation error in a single pass.

**Prerequisites:**
- A TOML file at the path passed via the global `--config` flag (lives on the top-level parser, not the `config check` subparser — `src/lcsas/cli/main.py:55`).
- Paths referenced in the TOML must exist and be the correct type for a clean run.

**Steps:**
1. `lcsas --config PATH config check` — load and validate. (`src/lcsas/cli/main.py:818`)
   - Parser: `src/lcsas/cli/main.py:372`.
   - Missing `--config` logs `--config is required for config check.` and returns 1 (`src/lcsas/cli/main.py:822`).
2. `load_config()` parses via `tomllib`, warns on unknown sections/keys, resolves relative paths, and builds a frozen `LCSASConfig`. (`src/lcsas/config/settings.py:119`)
3. `validate_config()` checks (`src/lcsas/config/settings.py:258`):
   - `mirror_base_path` exists and is a directory (`:266`).
   - `staging_path` exists, is a directory, and is writable (`:276`).
   - `db_path` parent exists and is writable (`:290`).
   - `default_ecc_redundancy_pct` in `[0, 100]` (`:301`).
   - `metadata_reserve_bytes` non-negative and `< default_media_type.usable_bytes` (`:308`).
   - `label_prefix` non-empty, matches `[A-Z0-9_]+`, short enough for a 32-char ISO 9660 label (`:322`).
   - Per-repo `mirror_path` exists and is a directory; `password_file` exists if set (`:342`).
   - `staging_path` and `mirror_base_path` are not identical or nested (cleanup would destroy mirrors) (`:361`).

**Expected outcome:**
- Valid: one `Configuration is valid.` log line, exit 0.
- Invalid: one log line per error, exit 1. All errors reported in one pass.

**Variant axes that apply:**
- Media type: `defaults.media_type` gates `metadata_reserve_bytes` against `usable_bytes`; test media (`TEST_TINY`/`TEST_SMALL`/`TEST_CD`) accepted (`src/lcsas/config/media.py:26`).
- Multi-tenant: each `[repos.<name>]` block validated independently; one error per failing repo.
- OS: filesystem semantics of `Path.resolve()` and `os.access(..., W_OK)` matter; read-only mounts trip `staging_path is not writable`.
- Optical drive count: N/A — `optical_device` is parsed but **not** validated (typos surface only at burn time).
- Multi-copy: N/A.
- ECC: `default_ecc_redundancy_pct` range-checked only.
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
- Parser / dispatch / handler: `src/lcsas/cli/main.py:372`, `:2677`, `:818`.
- Loader / validator / default-config factory: `src/lcsas/config/settings.py:119`, `:258`, `:243`.
- Unknown-key warning whitelist: `src/lcsas/config/settings.py:78`.

---

## `lcsas db export`

**Purpose:** Emit a JSON dump of top-line counts, all volumes, and all repositories to stdout.

**Prerequisites:**
- A reachable SQLite catalog at `--db` (or the config's `paths.database`, or `archive.db` in cwd). The handler calls `create_all()` defensively, so a missing-but-creatable path is initialized as a side effect (`src/lcsas/cli/main.py:797`).

**Steps:**
1. `lcsas [--db PATH | --config PATH] db export` — open the DB and emit JSON. (`src/lcsas/cli/main.py:786`)
   - Parser: `src/lcsas/cli/main.py:358`. DB path resolution (`--db` > config > `archive.db`): `src/lcsas/cli/main.py:403`.
2. Calls `get_archive_status_summary()`, `list_volumes()`, `list_repos()`; serializes via `json.dumps(..., indent=2)`.

**Expected outcome:**
- Stdout JSON object with keys `status`, `volumes`, `repositories`.
- Volume entries: `label`, `media_type`, `status`, `location`. Repo entries: `repo_id`, `name`, `mirror_path`.
- Exit 0.

**Variant axes that apply:**
- Media type: N/A — media is dumped verbatim from `volumes.media_type`.
- Multi-tenant: each repo appears once; encryption keys are **not** included.
- OS: pure-Python, no platform behavior.
- Optical drive count: N/A.
- Multi-copy: volumes appear once, but `volume_copies` rows are omitted — multi-copy state is invisible here.
- ECC: N/A.
- Recovery tier: Tier 0.

**Test coverage:**
- Existing:
  - `tests/unit/test_cli.py::TestCLIParsing::test_db_export` — argparse wiring.
  - `tests/unit/test_cli_handlers.py::TestCmdDbExport::test_db_export_json` — keys present, repos appear.
  - `tests/unit/test_cli_comprehensive.py::TestCmdDbExportEdges::test_export_has_all_keys` — volumes round-trip.
- Gaps:
  - No test asserts that `volume_copies`, `sessions`, `locations`, `snapshots`, `volume_events` are intentionally excluded.
  - No `lcsas db import` exists — one-way export only; operators must copy the SQLite file for a true backup (real gap).
  - No JSON-schema/contract test for the export shape.

**Source refs:**
- Parser / dispatch / handler: `src/lcsas/cli/main.py:358`, `:2675`, `:786`.
- DB path resolver: `src/lcsas/cli/main.py:403`.
- Status summary helper: `src/lcsas/db/queries.py::get_archive_status_summary`.

---

## Notes & gaps

Observations from reading the source; **not** fixes.

- **No `lcsas db import`.** Only `db export` is wired (`src/lcsas/cli/main.py:360`). True backups require copying the raw `.sqlite` file.
- **`init` honors `--config`.** `lcsas --config foo.toml init` writes to the TOML's `paths.database` (resolution order: `--db-path` > `--db` > `--config` > `./archive.db`) — fixed in issue #17 (`src/lcsas/cli/main.py:452`).
- **`init` does not migrate.** `create_all` stamps `CURRENT_SCHEMA_VERSION` only when `schema_version` is empty (`src/lcsas/db/schema.py:189`); migrations happen on access via `migrate()` (`src/lcsas/db/schema.py:200`).
- **`config check` does not validate `optical_device`** — typos surface only at burn time.
- **`--config` is a top-level flag.** `lcsas config check --config foo.toml` fails argparse; correct form is `lcsas --config foo.toml config check`. The error message could be clearer about position.
- **Unknown TOML keys are warnings, not errors.** A typo-quiet config can load "successfully" and silently produce nothing on `scan` (`src/lcsas/config/settings.py:78`).
- **`db export` is not a backup.** It omits packs, snapshots, sessions, locations, volume_copies, and audit trail. A rename or a true `db dump` may be warranted.
