# Multi-Tenant Repository Management

LCSAS is multi-tenant: a single catalog manages many independent Rustic
repositories. Tenants share physical storage at every tier — staging
tree, ISOs, optical discs — but remain cryptographically isolated
because every pack is rustic-encrypted with the repo's own key before
LCSAS sees it. The catalog tags every pack, snapshot, and volume link
with `repo_id` so burn, restore, and consolidation scope cleanly.

The `repositories` table (schema v5) holds `repo_id` (UUID), `name`,
`mirror_path`, and `encryption_key_id` (auto-detected from the mirror's
`keys/` directory — `src/lcsas/utils/fs.py:132`). The TOML config
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

1. `lcsas repo add <name> <mirror_path> [--key-file PATH]` — generate
   UUID, scan `keys/` for the first key filename, INSERT into
   `repositories`. (`src/lcsas/cli/main.py:464`)
2. `register_repo()` writes `repo_id`, `name`, absolute `mirror_path`,
   and `encryption_key_id`. (`src/lcsas/db/repos.py:25`)
3. `--key-file` is parsed but **not** persisted — only the
   auto-detected key ID is written. The password file is supplied via
   TOML config. (`src/lcsas/cli/main.py:82`, `src/lcsas/utils/fs.py:132`)

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
  - `--key-file` is accepted but silently ignored; no test documents
    this.
  - No test rejects/warns when `mirror_path` has no `keys/` — the
    empty `encryption_key_id` silently breaks later `KEY_INFO.txt`.

**Source refs:**

- CLI parser: `src/lcsas/cli/main.py:76-90`
- Handler: `src/lcsas/cli/main.py:464-491`
- DB insert: `src/lcsas/db/repos.py:25-39`
- Key-ID auto-detection: `src/lcsas/utils/fs.py:132-143`
- Model: `src/lcsas/db/models.py:41-49`

---

## Enumerate registered repositories (`lcsas repo list`)

**Purpose:** Print all registered tenants and mirror paths; used to
resolve repo names to UUIDs.

**Prerequisites:** Catalog exists (handler auto-runs `create_all`).

**Steps:**

1. `lcsas repo list` — fetch all rows from `repositories` ordered by
   name, print one line per repo. (`src/lcsas/cli/main.py:494`)
2. `list_repos()` runs `SELECT * FROM repositories ORDER BY name`
   and maps rows to frozen `Repository` dataclasses.
   (`src/lcsas/db/repos.py:52-55`)
3. Format: `<name>  <uuid>  <mirror_path>`. Empty catalog emits
   `No repositories registered.` and exits 0.
   (`src/lcsas/cli/main.py:508-513`)

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

- CLI parser: `src/lcsas/cli/main.py:85`
- Handler: `src/lcsas/cli/main.py:494-514`
- DB query: `src/lcsas/db/repos.py:52-55`

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
   with `not found` if missing. (`src/lcsas/cli/main.py:517-534`)
2. List active (non-pruned) packs. Refuse without `--force` if any are
   linked to active volumes, then refuse without `--force` if any
   active packs exist at all. (`src/lcsas/cli/main.py:537-558`)
3. With `--force`, prompt `Type 'yes' to confirm`; EOF on stdin
   returns exit 1. (`src/lcsas/cli/main.py:561-580`)
4. `bulk_mark_pruned` active packs → `DELETE FROM volume_packs` per
   pack → `DELETE FROM packs WHERE repo_id = ?` → delete snapshots →
   delete the `repositories` row. (`src/lcsas/cli/main.py:582-602`,
   `src/lcsas/db/repos.py:58-74`)
5. Whole teardown runs inside `locked_connection` (single transaction).
   (`src/lcsas/cli/main.py:527`)

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

- CLI parser: `src/lcsas/cli/main.py:87-90`
- Handler: `src/lcsas/cli/main.py:517-608`
- DB delete (with pack-count guard): `src/lcsas/db/repos.py:58-74`
- Snapshot cascade: `src/lcsas/db/snapshots.py` (`delete_snapshots_for_repo`)

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
   (`src/lcsas/burn/orchestrator.py:487-497`)
2. `HolographicInjector.inject_metadata` copies `index/`, `snapshots/`,
   `keys/`, and `config` from each repo's mirror into
   `<staging>/metadata/<repo_id>/`. The rustic key file is itself
   password-encrypted. (`src/lcsas/staging/metadata.py:35-59`,
   `src/lcsas/utils/pack_layout.py:24`)
3. `HolographicInjector.write_key_info` renders `KEY_INFO.txt` listing
   each repo's key ID and key filename for the human reader.
   (`src/lcsas/staging/metadata.py:348-394`)
4. `SubprocessRusticRunner._run` passes `--password-file <path>` per
   call; the path is scrubbed from error output via
   `mask_password_path`. (`src/lcsas/rustic/wrapper.py:74-118`)

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

- Mirror-path enumeration: `src/lcsas/burn/orchestrator.py:487-497`
- Metadata injection: `src/lcsas/staging/metadata.py:35-59`
- Metadata subdir list: `src/lcsas/utils/pack_layout.py:24`
- KEY_INFO renderer: `src/lcsas/staging/metadata.py:348-394`
- Rustic password handling: `src/lcsas/rustic/wrapper.py:74-118`
- Password masking: `src/lcsas/log.py` (`mask_password_path`)

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
   `--password-file <repo's file>`. (`src/lcsas/rustic/wrapper.py:82-89`)
2. Packs on the mirror are already rustic-encrypted when LCSAS picks
   them up and are content-addressed by ciphertext hash; LCSAS never
   decrypts. (`src/lcsas/packs/scanner.py`)
3. The catalog scopes every `packs` and `snapshots` row by `repo_id`,
   so `get_unarchived_packs(repo_id=...)` and restore planning are
   tenant-scoped. (`src/lcsas/db/queries.py`,
   `tests/unit/test_multi_tenant.py`)
4. Bin-packing mixes tenants on a disc but keeps each repo's metadata
   under its own `<staging>/metadata/<repo_id>/`.
   (`src/lcsas/burn/orchestrator.py:366-384`)

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
  (`src/lcsas/staging/metadata.py:367-394`,
  `src/lcsas/staging/metadata.py:425-432`)
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

- Catalog scoping: `src/lcsas/db/models.py:30-49`,
  `src/lcsas/db/queries.py`
- Bin-pack groups by repo at stage time:
  `src/lcsas/burn/orchestrator.py:366-384`
- Per-disc per-tenant metadata trees:
  `src/lcsas/staging/metadata.py:35-59`
- Survivability disclosure surface:
  `src/lcsas/staging/metadata.py:348-444`
- Rustic password isolation:
  `src/lcsas/rustic/wrapper.py:74-118`
