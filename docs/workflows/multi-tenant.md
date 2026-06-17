# Multi-Tenant Repository Management

LCSAS is multi-tenant: a single catalog manages many independent Rustic
repositories. Tenants share physical storage at every tier — staging
tree, ISOs, optical discs — but remain cryptographically isolated
because every pack is rustic-encrypted with the repo's own key before
LCSAS sees it. The catalog tags every pack, snapshot, and volume link
with `repo_id` so burn, restore, and consolidation scope cleanly.

The `repositories` table (schema v9) holds `repo_id` (UUID), `name`,
`mirror_path`, and `encryption_key_id` (auto-detected from the mirror's
`keys/` directory — `read_repo_key_ids` in `src/lcsas/utils/fs.py`). The TOML config
supplies the rustic `password_file` per repo; the password is never
stored in the catalog or written to disc.

## Table of contents

1. [Register a repository (`lcsas repo add`)](#register-a-repository-lcsas-repo-add)
2. [Enumerate registered repositories (`lcsas repo list`)](#enumerate-registered-repositories-lcsas-repo-list)
3. [Remove a repository (`lcsas repo remove`)](#remove-a-repository-lcsas-repo-remove)
4. [Per-repo key handling on shared volumes](#per-repo-key-handling-on-shared-volumes)
5. [Cross-repo isolation guarantees](#cross-repo-isolation-guarantees)

---

## Register a repository (`lcsas repo add`)

**Purpose:** Insert a new tenant row and auto-detect its rustic
encryption key ID from the mirror's `keys/` directory.

**Prerequisites:**

- LCSAS catalog initialized (`lcsas init`).
- Existing rustic/restic repo on local disk with `config` file and
  `keys/` directory (any `rustic init` produces these).
- Mirror path readable by the LCSAS user.

**Steps:**

1. `lcsas repo add <name> <mirror_path>` — generate UUID, scan
   `keys/` for the first key filename, INSERT into `repositories`.
   (`cmd_repo_add` in `src/lcsas/cli/main.py`)
2. `register_repo()` writes `repo_id`, `name`, absolute `mirror_path`,
   and `encryption_key_id`. (`register_repo` in `src/lcsas/db/repos.py`)
3. The encryption key ID is auto-detected from `mirror_path/keys/`.
   The password file is supplied via TOML config.
   (`read_repo_key_ids` in `src/lcsas/utils/fs.py`)

**Expected outcome:** New row with fresh UUID and absolute mirror path;
log line `Registered repository '<name>' (id: <uuid>)`. Subsequent
`lcsas scan --repo <name>` can associate packs with this tenant.

**Variant axes that apply:**

- Multi-tenant: entry point for multi-tenancy; behavior identical for
  the Nth repo as for the first.
- All others (Media / OS / Drives / Multi-copy / ECC / Recovery tier):
  N/A — pure catalog mutation.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli.py::TestRepoCommands::test_repo_add` —
    dispatch smoke test.
  - `tests/unit/test_cli_comprehensive.py::TestCmdRepoAddEdges::test_duplicate_repo_name_errors`
    — duplicate names get distinct UUIDs.
  - `tests/unit/test_multi_tenant.py::test_repos_registered_independently`
    — five tenants registered directly.
- Gaps:
  - No CLI-level test asserts `encryption_key_id` is populated from a
    real `keys/` layout.
  - No test rejects/warns when `mirror_path` has no `keys/` — the
    empty `encryption_key_id` silently breaks later `KEY_INFO.txt`.

**Source refs:**

- CLI parser: `build_parser()` (`repo add`) in `src/lcsas/cli/main.py`
- Handler: `cmd_repo_add` in `src/lcsas/cli/main.py`
- DB insert: `register_repo` in `src/lcsas/db/repos.py`
- Key-ID auto-detection: `read_repo_key_ids` in `src/lcsas/utils/fs.py`
- Model: `Repository` in `src/lcsas/db/models.py`
- Architecture overview: [`docs/architecture.md`](../architecture.md)

---

## Enumerate registered repositories (`lcsas repo list`)

**Purpose:** Print all registered tenants and mirror paths; used to
resolve repo names to UUIDs.

**Prerequisites:** Catalog exists (handler auto-runs `create_all`).

**Steps:**

1. `lcsas repo list` — fetch all rows from `repositories` ordered by
   name, print one line per repo. (`cmd_repo_list` in `src/lcsas/cli/main.py`)
2. `list_repos()` runs `SELECT * FROM repositories ORDER BY name`
   and maps rows to frozen `Repository` dataclasses.
   (`list_repos` in `src/lcsas/db/repos.py`)
3. Format: `<name>  <uuid>  <mirror_path>`. Empty catalog emits
   `No repositories registered.` and exits 0.
   (`cmd_repo_list` in `src/lcsas/cli/main.py`)

**Expected outcome:** Tenants listed alphabetically; exit 0 in all
cases.

**Variant axes that apply:**

- Multi-tenant: directly exercises catalog enumeration.
- All others: N/A.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli.py::TestRepoCommands::test_repo_list`
  - `tests/unit/test_cli_comprehensive.py::TestCmdRepoListEdges::test_many_repos`
  - `tests/unit/test_multi_tenant.py::test_repo_retrieval_by_id`
- Gaps:
  - No assertion of stable / locale-independent sort order.
  - Empty-catalog branch untested.
  - No `--json` output; operators must parse formatted log lines.

**Source refs:**

- CLI parser: `build_parser()` (`repo list`) in `src/lcsas/cli/main.py`
- Handler: `cmd_repo_list` in `src/lcsas/cli/main.py`
- DB query: `list_repos` in `src/lcsas/db/repos.py`

---

## Remove a repository (`lcsas repo remove`)

**Purpose:** Delete a tenant from the catalog, optionally pruning its
packs and cascade-deleting snapshots and `volume_packs` links. No
separate `deprecate` subcommand exists for repos; the DEPRECATED state
lives on `volumes` (managed by `consolidate --deprecate`).

**Prerequisites:**

- A registered repo (UUID from `lcsas repo list`).
- For `--force`: interactive TTY (`yes` confirmation read from stdin).

**Steps:**

1. `lcsas repo remove <repo_id> [--force]` — look up by UUID; exit 1
   with `not found` if missing. (`cmd_repo_remove` in `src/lcsas/cli/main.py`)
2. List active (non-pruned) packs. Refuse without `--force` if any are
   linked to active volumes, then refuse without `--force` if any
   active packs exist at all. (`cmd_repo_remove` in `src/lcsas/cli/main.py`)
3. With `--force`, prompt `Type 'yes' to confirm`; EOF on stdin
   returns exit 1. (`cmd_repo_remove` in `src/lcsas/cli/main.py`)
4. `bulk_mark_pruned` active packs → `DELETE FROM volume_packs` per
   pack → `DELETE FROM packs WHERE repo_id = ?` → delete snapshots →
   delete the `repositories` row. (`cmd_repo_remove` in
   `src/lcsas/cli/main.py`, `delete_repo` in `src/lcsas/db/repos.py`)
5. Whole teardown runs inside `locked_connection` (single transaction).
   (`cmd_repo_remove` in `src/lcsas/cli/main.py`)

**Expected outcome:**

- Without `--force` on a repo with packs or volume links: exit 1 with a
  message naming how many block removal.
- With `--force` after confirmation: row and all FK-related rows for
  `repo_id` gone.
- Mirror's key file is **not** touched. Packs already burned to optical
  discs are **not** touched (still decryptable by anyone with the key).

**Variant axes that apply:**

- Multi-tenant: one tenant must leave others intact
  (`test_deleting_repo_does_not_affect_others`).
- Recovery tier: catalog removal does **not** purge Tier-2 optical
  copies — intentional, operator-relevant.
- All others: N/A.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli_comprehensive.py::TestCmdRepoRemove::test_remove_nonexistent_repo`
  - `tests/unit/test_cli_comprehensive.py::TestCmdRepoRemove::test_remove_empty_repo`
  - `tests/unit/test_cli_comprehensive.py::TestCmdRepoRemove::test_remove_with_packs_needs_force`
  - `tests/unit/test_multi_tenant.py::test_deleting_repo_does_not_affect_others`
- Gaps:
  - `--force` interactive confirmation path untested (needs
    `builtins.input` monkeypatch).
  - "Packs on active volumes" branch not asserted distinct from "active
    packs".
  - EOFError path (piped automation) untested.
  - No soft-deprecate at the repo level; operators retiring a tenant
    have no option short of full removal.

**Source refs:**

- CLI parser: `build_parser()` (`repo remove`) in `src/lcsas/cli/main.py`
- Handler: `cmd_repo_remove` in `src/lcsas/cli/main.py`
- DB delete: `delete_repo` in `src/lcsas/db/repos.py`
- Snapshot cascade: `delete_snapshots_for_repo` in `src/lcsas/db/snapshots.py`

---

## Per-repo key handling on shared volumes

**Purpose:** Document how each tenant's key material flows onto every
disc (enabling per-disc standalone restore) without leaking one
tenant's key into another's tree, and without ever putting the user's
password on disc.

**Prerequisites:** Each repo registered, and its `password_file` set
in TOML under `[repos.<name>]`. The password file stays on the
operator's filesystem — never in staging or on disc.

**Steps:**

1. `BurnOrchestrator._get_mirror_paths()` builds `{repo_id: mirror_path}`
   from **every** row in `repositories`.
   (`BurnOrchestrator._get_mirror_paths` in `src/lcsas/burn/orchestrator.py`)
2. `HolographicInjector.inject_metadata` copies `index/`, `snapshots/`,
   `keys/`, and `config` from each repo's mirror into
   `<staging>/metadata/<repo_id>/`. The rustic key file is itself
   password-encrypted. (`HolographicInjector.inject_metadata` in
   `src/lcsas/staging/metadata.py`, `METADATA_SUBDIRS` in
   `src/lcsas/utils/pack_layout.py`)
3. `HolographicInjector.write_key_info` renders `KEY_INFO.txt` listing
   each repo's key ID and key filename for the human reader.
   (`HolographicInjector.write_key_info` in `src/lcsas/staging/metadata.py`)
4. `SubprocessRusticRunner._run` passes `--password-file <path>` per
   call; the path is scrubbed from error output via
   `mask_password_path`. (`SubprocessRusticRunner._run` in
   `src/lcsas/rustic/wrapper.py`)

**Expected outcome:**

- Every disc carries a `metadata/<repo_id>/` subtree for every
  registered repo, regardless of which repos have packs on this disc.
- `KEY_INFO.txt` names each repo's key ID and key filename — never the
  password.
- Rustic error logs never include the password file path.

**Variant axes that apply:**

- Multi-tenant: the only relevant axis.
- Media / Multi-copy: irrelevant — identical metadata layout
  everywhere.
- Recovery tier: makes Tier-2 self-describing per tenant.

**Test coverage:**

- Existing:
  - `tests/unit/test_staging.py::test_inject_metadata`
  - `tests/unit/test_staging.py::test_write_key_info_with_repos`
- Gaps:
  - No test pins the "inject **all** repos even when only one has
    packs on the volume" contract; would silently regress if
    `_get_mirror_paths` changed.
  - No test verifies the user's password contents never appear in
    staging output. `mask_password_path` is tested but not within the
    staging pipeline.
  - No assertion on `metadata/<repo_id>/keys/` permissions.

**Source refs:**

- Mirror-path enumeration: `BurnOrchestrator._get_mirror_paths` in `src/lcsas/burn/orchestrator.py`
- Metadata injection: `HolographicInjector.inject_metadata` in `src/lcsas/staging/metadata.py`
- Metadata subdir list: `METADATA_SUBDIRS` in `src/lcsas/utils/pack_layout.py`
- KEY_INFO renderer: `HolographicInjector.write_key_info` in `src/lcsas/staging/metadata.py`
- Rustic password handling: `SubprocessRusticRunner._run` in `src/lcsas/rustic/wrapper.py`
- Password masking: `mask_password_path` in `src/lcsas/log.py`

---

## Cross-repo isolation guarantees

**Purpose:** Specify what LCSAS does — and does **not** — guarantee when
multiple tenants share a physical volume.

**Prerequisites:** Two or more repos with **distinct** rustic passwords.
LCSAS does not enforce password distinctness.

**Mechanism:**

1. Each repo is `rustic init`-ed with its own password before LCSAS
   sees it; rustic stores a password-wrapped master key in
   `<mirror>/keys/<id>`. LCSAS only ever invokes rustic with
   `--password-file <repo's file>`. (`SubprocessRusticRunner._run` in
   `src/lcsas/rustic/wrapper.py`)
2. Packs on the mirror are already rustic-encrypted when LCSAS picks
   them up and are content-addressed by ciphertext hash; LCSAS never
   decrypts. (`scan_mirror_packs` in `src/lcsas/packs/scanner.py`)
3. The catalog scopes every `packs` and `snapshots` row by `repo_id`,
   so `get_unarchived_packs(repo_id=...)` and restore planning are
   tenant-scoped. (`get_unarchived_packs` in `src/lcsas/db/queries.py`,
   `tests/unit/test_multi_tenant.py`)
4. Bin-packing mixes tenants on a disc but keeps each repo's metadata
   under its own `<staging>/metadata/<repo_id>/`.
   (`BurnOrchestrator` in `src/lcsas/burn/orchestrator.py`)

**Guarantees:**

- **Cryptographic isolation:** repo A's password cannot decrypt repo
  B's packs or read repo B's snapshots even on a shared disc. Rustic
  is the sole mechanism; LCSAS adds no additional layer.
- **Catalog isolation:** every pack-level query carries `repo_id`;
  `repo remove --force` does not perturb others
  (`test_deleting_repo_does_not_affect_others`).
- **Restore isolation:** `RestorePlanner` scopes pick lists by snapshot,
  which is scoped by `repo_id`.

**Non-guarantees (known gaps):**

- **Existence side-channel:** every disc names every registered repo
  in `KEY_INFO.txt` / `CONFIG_SUMMARY.txt` and carries a
  `metadata/<repo_id>/` subtree even with zero packs from that repo.
  One disc reveals all tenant names.
  (`HolographicInjector.write_key_info` and
  `HolographicInjector.write_config_summary` in
  `src/lcsas/staging/metadata.py`)
- **Pack-size side-channel:** `catalog.db` on every disc enumerates
  `(pack_id, repo_id, size_bytes)` for **all** packs across all
  tenants. Any single disc leaks every other tenant's backup size and
  count.
- **Shared-password risk:** nothing prevents two repos from sharing a
  `password_file`; if they do, "isolation" is just naming.
- **Key file on disc:** the password-encrypted rustic key file is on
  every disc. Weak passwords are crackable offline from any disc — a
  rustic property amplified by holographic metadata.
- **`encryption_key_id` is advisory:** captured from the first
  `keys/` filename at `repo add` time; key rotation does not update
  the catalog.

**Variant axes that apply:**

- Multi-tenant: the whole concern.
- Recovery tier: must hold at Tier-0, Tier-1, and Tier-2.
- All others: N/A.

**Test coverage:**

- Existing: `tests/unit/test_multi_tenant.py` (entire file —
  `test_packs_scoped_to_repo`, `test_unarchived_scoped_to_repo`,
  `test_archiving_one_repo_leaves_other_unarchived`,
  `test_pick_list_single_repo_packs`, etc.);
  `tests/unit/test_staging.py::test_write_key_info_with_repos`.
- Gaps:
  - No test pins the "all repos' metadata go on every disc" contract.
  - No test of cross-tenant restore refusal (repo A's password vs.
    repo B's metadata).
  - Catalog-on-disc as a side channel is not in the threat model.
  - No test rejects or warns when two repos share a `password_file`.

**Source refs:**

- Catalog scoping: `Repository` in `src/lcsas/db/models.py`,
  `get_unarchived_packs` in `src/lcsas/db/queries.py`
- Bin-pack groups by repo at stage time:
  `BurnOrchestrator` in `src/lcsas/burn/orchestrator.py`
- Per-disc per-tenant metadata trees:
  `HolographicInjector.inject_metadata` in `src/lcsas/staging/metadata.py`
- Survivability disclosure surface:
  `HolographicInjector.write_key_info` /
  `HolographicInjector.write_config_summary` in
  `src/lcsas/staging/metadata.py`
- Rustic password isolation:
  `SubprocessRusticRunner._run` in `src/lcsas/rustic/wrapper.py`
- Architecture overview: [`docs/architecture.md`](../architecture.md)
