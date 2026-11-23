# Multi-Repository Management

A single LCSAS catalog manages many independent Rustic repositories.
Repositories share storage at every stage — the staging area, ISO
images, discs — but remain cryptographically isolated because every
pack is rustic-encrypted with the repository's own key before LCSAS
sees it. The catalog tags every pack, snapshot, and volume link
with `repo_id` so burn, restore, and consolidation scope cleanly.

The `repositories` table (schema v10) holds `repo_id` (UUID), `name`,
`mirror_path`, `encryption_key_id` (auto-detected from the mirror's
`keys/` directory — `read_repo_key_ids` in `src/lcsas/utils/fs.py`), and
`status` (`active` | `retired` — see
[Retire a repository](#retire-a-repository-lcsas-repo-retire)). The TOML config
supplies the rustic `password_file` per repository; the password is never
stored in the catalog or written to disc.

## Table of contents

1. [Register a repository (`lcsas repo add`)](#register-a-repository-lcsas-repo-add)
2. [Enumerate registered repositories (`lcsas repo list`)](#enumerate-registered-repositories-lcsas-repo-list)
3. [Retire a repository (`lcsas repo retire`)](#retire-a-repository-lcsas-repo-retire)
4. [Remove a repository (`lcsas repo remove`)](#remove-a-repository-lcsas-repo-remove)
5. [Per-repository key handling on shared volumes](#per-repository-key-handling-on-shared-volumes)
6. [Cross-repository isolation guarantees](#cross-repository-isolation-guarantees)

---

## Register a repository (`lcsas repo add`)

**Purpose:** Insert a new repository row and auto-detect its rustic
key ID from the mirror's `keys/` directory.

**Prerequisites:**

- LCSAS catalog initialized (`lcsas init`).
- Existing rustic/restic repository on local disk with `config` file and
  `keys/` directory (any `rustic init` produces these).
- Mirror path readable by the account that runs LCSAS.

**Steps:**

1. `lcsas repo add <name> <mirror_path>` — scan `keys/` for the first
   key filename; refuse a mirror that is a **retired** repository (its
   recorded key is in the mirror's `keys/`, or, when either side has no key, its
   mirror path is the same): the error names that repository and offers
   `lcsas repo activate <name>`, because its packs are on discs under its
   catalog ID and a new ID would burn every pack again (#962). Then refuse
   a name or mirror path another repository already has (the error names
   that repository; the schema has no UNIQUE index, so the command checks,
   #685), generate the UUID, INSERT into `repositories`.
   (`cmd_repo_add` in `src/lcsas/cli/commands/repo.py`)
2. `register_repo()` writes `repo_id`, `name`, absolute `mirror_path`,
   and `encryption_key_id`. (`register_repo` in `src/lcsas/db/repos.py`)
3. The key ID is auto-detected from `mirror_path/keys/`.
   The password file is supplied via TOML config.
   (`read_repo_key_ids` in `src/lcsas/utils/fs.py`)

**Expected outcome:** New row with fresh UUID and absolute mirror path;
log line `Registered repository '<name>' (id: <uuid>)`. Subsequent
`lcsas scan --repo <name>` can associate packs with this repository.

**One repository is registered once.** The catalog ID minted here is the
repository's identity for its whole life: it is on every disc
(`metadata/<repo_id>/`, `packs.repo_id` in the disc catalog). It is not
the repository's own Rustic id, so LCSAS cannot tell two mirrors of one
repository apart from two repositories. A copy (clone) of a repository's
mirror registered as a second repository is not supported: a pack ID is
unique across the catalog, so the second registration finds every pack
already owned by the first and gets none. Register each repository once;
if its mirror moves, keep its catalog row. A retired repository is
brought back with `lcsas repo activate`, never with `repo add`; nothing
yet updates a recorded mirror path, so put a moved mirror back at the path
`lcsas repo list` shows before activating it.

**Variant axes that apply:**

- Multi-repository: entry point for an archive with several repositories;
  behavior identical for the Nth repository as for the first.
- All others (Media / OS / Drives / Multi-copy / ECC / Storage stage):
  N/A — pure catalog mutation.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli.py::TestCLIParsing::test_repo_add` —
    dispatch smoke test.
  - `tests/unit/test_cli_repo.py::TestCmdRepoAddEdges::test_duplicate_repo_name_errors`
    and `tests/unit/test_repo_resolution_and_key_records.py` — a duplicate
    name or mirror path is refused (#685).
  - `tests/unit/test_multi_tenant.py::test_repos_registered_independently`
    — five repositories registered directly.
  - `tests/integration/test_repo_remove_guard.py::test_repo_add_of_a_retired_repository_offers_activate`
    — the retired repository's mirror, at its path or copied elsewhere, is
    refused with the `repo activate` hint; activate keeps its ID and
    queues no pack again (#962).
- Gaps:
  - No CLI-level test asserts `encryption_key_id` is populated from a
    real `keys/` layout.
  - No test rejects/warns when `mirror_path` has no `keys/` — the
    empty `encryption_key_id` silently breaks later `PASSWORD_INFO.txt`.

**Source refs:**

- CLI parser: `build_parser()` (`repo add`) in `src/lcsas/cli/parser.py`
- Handler: `cmd_repo_add` in `src/lcsas/cli/commands/repo.py`
- Catalog insert: `register_repo` in `src/lcsas/db/repos.py`
- Key-ID auto-detection: `read_repo_key_ids` in `src/lcsas/utils/fs.py`
- Model: `Repository` in `src/lcsas/db/models.py`
- Architecture overview: [`docs/architecture.md`](../architecture.md)

---

## Enumerate registered repositories (`lcsas repo list`)

**Purpose:** Print all registered repositories and mirror paths; used to
resolve repository names to UUIDs.

**Prerequisites:** Catalog exists (handler auto-runs `create_all`).

**Steps:**

1. `lcsas repo list` — fetch all rows from `repositories` ordered by
   name, print one line per repository. (`cmd_repo_list` in `src/lcsas/cli/commands/repo.py`)
2. `list_repos()` runs `SELECT * FROM repositories ORDER BY name`
   and maps rows to frozen `Repository` dataclasses.
   (`list_repos` in `src/lcsas/db/repos.py`)
3. Format: `<name>  <status>  <uuid>  <mirror_path>`. Empty catalog emits
   `No repositories registered.` and exits 0.
   (`cmd_repo_list` in `src/lcsas/cli/commands/repo.py`)

**Expected outcome:** Repositories listed alphabetically; exit 0 in all
cases.

**Variant axes that apply:**

- Multi-repository: directly exercises catalog enumeration.
- All others: N/A.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli.py::TestCLIParsing::test_repo_list`
  - `tests/unit/test_cli_repo.py::TestCmdRepoListEdges::test_many_repos`
  - `tests/unit/test_multi_tenant.py::test_repo_retrieval_by_id`
- Gaps:
  - No assertion of stable / locale-independent sort order.
  - Empty-catalog branch untested.
  - No `--json` output; the owner must parse formatted log lines.

**Source refs:**

- CLI parser: `build_parser()` (`repo list`) in `src/lcsas/cli/parser.py`
- Handler: `cmd_repo_list` in `src/lcsas/cli/commands/repo.py`
- Catalog query: `list_repos` in `src/lcsas/db/repos.py`

---

## Retire a repository (`lcsas repo retire`)

**Purpose:** Record that a repository's Rustic mirror is gone *for good*, as
opposed to merely unmounted right now. LCSAS cannot tell those two apart
from `mirror_path` alone, and the difference matters: `lcsas meta build`
**fails** when a live repository whose packs are on discs got no Rustic keys
bundled, and retiring is what exempts a repository whose mirror is legitimately
gone (see [meta-disc.md](meta-disc.md#key-gate-when-a-missing-repo-key-fails-the-build)).
Retiring is how the owner answers that question once, in the catalog,
instead of passing an override flag on every build.

Retirement is **not** removal. `lcsas repo remove` deletes a repository
from the catalog, so it refuses one with packs on burned volumes (#962);
retirement is the verb for such a repository. `retire` only flips a flag,
keeps every row, and is undone by `lcsas repo activate <repo>`. Because it
destroys nothing, it does not prompt for confirmation.

**Prerequisites:** A registered repository, given by its name or its
catalog ID (the UUID from `lcsas repo list`) (#581).

**Steps:**

1. `lcsas repo retire <repo>` — look up by catalog ID, then by name
   (exact, then any case; `_find_repo`); exit 1 with
   `Repository '<repo>' not found.` if absent, or name the clash if
   two repositories share the name.
   (`cmd_repo_retire` in `src/lcsas/cli/commands/repo.py`)
2. The handler opens a `locked_connection` and runs `ensure_schema`
   first, so a pre-v10 catalog (no `status` column) migrates in place
   rather than failing the `UPDATE`.
   (`_cmd_repo_set_status` in `src/lcsas/cli/commands/repo.py`)
3. `set_repo_status()` validates the value against `REPO_STATUSES` in
   Python — the column carries no SQL `CHECK`, because SQLite cannot
   retro-apply one through `ALTER TABLE ADD COLUMN` and a migrated
   catalog would silently lack it.
   (`set_repo_status` in `src/lcsas/db/repos.py`)
4. `lcsas repo activate <repo>` reverses it. Re-running either verb
   on a repository already in that state is a no-op that exits 0.

**Expected outcome:** `repo list` shows the new status; packs,
snapshots, and `volume_packs` links are untouched; `lcsas meta build`
stops failing on that repository's absent keys.

**What later discs say (#959).** Discs staged after a retirement do not
carry the retired set's metadata (only live repositories' metadata is
copied, and the mirror may be gone). So every later disc, in
`VOLUME_INFO.txt`, `volume_info.json` (`retired_backup_sets`) and
`START_HERE.txt`, and the estate card name each retired set and the disc
to start it from, and `repo retire` prints the same sentence:

    alpha: retired after 2026-11-23.  Its backups are on the discs up to
    LCSAS_BD25_2026_0010.  To restore it, use that disc as the "newest
    data disc".

The disc is the highest-numbered burned volume holding a pack of the set
(`retired_backup_sets` in `src/lcsas/db/repos.py`); the catalog records
no retirement date, so the date is that disc's and the sentence says
"after". Reprint the estate card after retiring a set.

**Also remove its `[repos.<name>]` block from the config.** Retirement is
a *catalog* fact, but mirror-existence validation is a *config* one:
`validate_config` (`src/lcsas/config/settings.py`) treats a missing
`mirror_path` as a hard error, and `scan`, `burn`, `verify`, and `status`
all gate on it via `_validate_config_or_exit`. So a repository that is retired
in the catalog but still configured will keep failing those commands with
`mirror_path does not exist`, even though `meta build` is now happy.
`lcsas repo retire` warns when it detects this, so the two halves cannot
silently drift apart.

**Nothing else reads `status`.** Not `scan`, not `burn`, not `status`,
not `estate` — retirement narrows exactly one gate and nothing more.

**Variant axes that apply:**

- Multi-repository: retire one repository, leave the others gated.
- All others: N/A.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli_repo_retirement.py`
  - `tests/unit/test_db_repos.py::TestRepoStatus`
  - `tests/unit/test_db_schema.py::TestMigrateV9ToV10`

**Source refs:**

- CLI parser: `build_parser()` (`repo retire` / `repo activate`) in `src/lcsas/cli/parser.py`
- Handlers: `cmd_repo_retire`, `cmd_repo_activate` in `src/lcsas/cli/commands/repo.py`
- Catalog write: `set_repo_status` in `src/lcsas/db/repos.py`
- Migration: `migrate()` v9 → v10 in `src/lcsas/db/schema.py`

---

## Remove a repository (`lcsas repo remove`)

**Purpose:** Delete a repository that never reached a burned volume — one
added by mistake — from the catalog, with its packs, snapshots,
`volume_packs` links and recorded split. A repository with packs on a
burned volume is never deleted: its catalog ID is on those discs, and
added again it would get a new ID and every pack would be burned a second
time under it (#962). Retire it instead. No separate `deprecate`
subcommand exists for repositories; the DEPRECATED state lives on
`volumes` (managed by `consolidate --deprecate`).

**Prerequisites:**

- A registered repository (its name, or the UUID from `lcsas repo list`).
- None of its packs, pruned or not, on a burned volume.
- For `--force`: interactive TTY (`yes` confirmation read from stdin).

**Steps:**

1. `lcsas repo remove <repo> [--force]` — look up by name or catalog ID
   (`_find_repo`); exit 1 with `not found` if missing. (`cmd_repo_remove` in `src/lcsas/cli/commands/repo.py`)
2. Refuse, with or without `--force`, when any of its packs (pruned
   included: a pruned pack is still on its discs) is on a burned volume:
   one that has left STAGING, or that has a disc recorded. The error
   counts and names those volumes, says nothing was deleted, and points
   at `lcsas repo retire <repo>`. (`_burned_volume_labels` in
   `src/lcsas/cli/commands/repo.py`)
3. Without `--force`, refuse when it has any pack in the catalog.
4. With `--force`, prompt `This deletes from the catalog: repository …,
   N pack(s) and M snapshot(s)`, then `Type 'yes' to confirm`; EOF on
   stdin returns exit 1.
5. Delete its `volume_packs` links and packs, recompute `used_bytes` of
   each staged volume that held them, delete its snapshots and recorded
   split, then the `repositories` row. Log `Deleted repository '<name>'
   from the catalog, with N pack(s) and M snapshot(s).`
   (`cmd_repo_remove` in `src/lcsas/cli/commands/repo.py`, `delete_repo`
   in `src/lcsas/db/repos.py`)

**Expected outcome:**

- A repository with packs on a burned volume: exit 1, catalog unchanged,
  whatever the flags.
- Without `--force` on a repository with packs: exit 1.
- With `--force` after confirmation: row and every row for its `repo_id`
  gone; staged volumes' `used_bytes` count only the packs they still hold.
- Mirror's key file is **not** touched. An ISO image already staged with
  its packs is not touched either: the command warns, naming the staged
  volumes, and `lcsas session abort` and a new `stage` rebuild the session
  without them. Burned as it is, such a disc would carry packs the catalog
  no longer records.

**Variant axes that apply:**

- Multi-repository: removing one repository must leave the others intact
  (`test_deleting_repo_does_not_affect_others`).
- Storage stage: catalog removal does **not** touch the discs, which is
  why a repository on burned discs is refused.
- All others: N/A.

**Test coverage:**

- Existing:
  - `tests/unit/test_cli_repo.py::TestCmdRepoRemoveEdges::test_remove_nonexistent_repo`
  - `tests/unit/test_cli_repo.py::TestCmdRepoRemove::test_repo_remove_happy_path`
  - `tests/unit/test_cli_repo.py::TestCmdRepoRemoveEdges::test_remove_with_packs_needs_force`
  - `tests/unit/test_cli_repo.py::TestCmdRepoRemove::test_repo_remove_rejects_packs_on_a_burned_volume`
  - `tests/unit/test_cli_repo.py::TestCmdRepoRemove::test_repo_remove_refuses_pruned_packs_on_a_burned_volume`
  - `tests/unit/test_cli_repo.py::TestCmdRepoRemove::test_repo_remove_with_force_purges_packs`
  - `tests/integration/test_repo_remove_guard.py` — real rustic and
    xorriso: a burned repository is refused with and without `--force`;
    a staged one is deleted with no orphan row and correct `used_bytes`.
  - `tests/unit/test_multi_tenant.py::test_deleting_repo_does_not_affect_others`
- Gaps:
  - EOFError path (piped automation) untested.

**Source refs:**

- CLI parser: `build_parser()` (`repo remove`) in `src/lcsas/cli/parser.py`
- Handler: `cmd_repo_remove`, `_burned_volume_labels` in `src/lcsas/cli/commands/repo.py`
- Catalog delete: `delete_repo` in `src/lcsas/db/repos.py`
- Snapshot cascade: `delete_snapshots_for_repo` in `src/lcsas/db/snapshots.py`

---

## Per-repository key handling on shared volumes

**Purpose:** Document how each repository's key material flows onto every
disc (so any one disc describes every repository) without leaking one
repository's key into another's tree, and without ever putting the
owner's password on disc.

**Prerequisites:** Each repository registered, and its `password_file` set
in TOML under `[repos.<name>]`. The password file stays on the
owner's filesystem — never in the staging area or on disc.

**Steps:**

1. `BurnOrchestrator._get_mirror_paths()` builds `{repo_id: mirror_path}`
   from **every** row in `repositories`.
   (`BurnOrchestrator._get_mirror_paths` in `src/lcsas/burn/orchestrator.py`)
2. `HolographicInjector.inject_metadata` copies `index/`, `snapshots/`,
   `keys/`, and `config` from each repository's mirror into
   `<staging>/metadata/<repo_id>/`. The rustic key file is itself
   password-encrypted. (`HolographicInjector.inject_metadata` in
   `src/lcsas/staging/metadata.py`, `METADATA_SUBDIRS` in
   `src/lcsas/utils/pack_layout.py`)
3. `HolographicInjector.write_key_info` renders `PASSWORD_INFO.txt` listing
   each repository's key ID and key filename for the human reader.
   (`HolographicInjector.write_key_info` in `src/lcsas/staging/metadata.py`)
4. `SubprocessRusticRunner._run` passes `--password-file <path>` per
   call; the path is scrubbed from error output via
   `mask_password_path`. (`SubprocessRusticRunner._run` in
   `src/lcsas/rustic/wrapper.py`)

**Expected outcome:**

- Every disc carries a `metadata/<repo_id>/` subtree for every
  registered repository, regardless of which repositories have packs on
  this disc.
- `PASSWORD_INFO.txt` names each repository's key ID and key filename — never the
  password.
- Rustic error logs never include the password file path.

**Variant axes that apply:**

- Multi-repository: the only relevant axis.
- Media / Multi-copy: irrelevant — identical metadata layout
  everywhere.
- Storage stage: makes every disc self-describing for every repository.

**Test coverage:**

- Existing:
  - `tests/unit/test_staging.py::test_inject_metadata`
  - `tests/unit/test_staging.py::test_write_key_info_with_repos`
- Gaps:
  - No test pins the "inject **all** repositories even when only one has
    packs on the volume" contract; would silently regress if
    `_get_mirror_paths` changed.
  - No test checks that the owner's password never appears in
    staging output. `mask_password_path` is tested but not within the
    staging pipeline.
  - No assertion on `metadata/<repo_id>/keys/` permissions.

**Source refs:**

- Mirror-path enumeration: `BurnOrchestrator._get_mirror_paths` in `src/lcsas/burn/orchestrator.py`
- Metadata injection: `HolographicInjector.inject_metadata` in `src/lcsas/staging/metadata.py`
- Metadata subdir list: `METADATA_SUBDIRS` in `src/lcsas/utils/pack_layout.py`
- PASSWORD_INFO renderer: `HolographicInjector.write_key_info` in `src/lcsas/staging/metadata.py`
- Rustic password handling: `SubprocessRusticRunner._run` in `src/lcsas/rustic/wrapper.py`
- Password masking: `mask_password_path` in `src/lcsas/log.py`

---

## Cross-repository isolation guarantees

**Purpose:** Specify what LCSAS does — and does **not** — guarantee when
several repositories share a volume.

**Prerequisites:** Two or more repositories with **distinct** rustic
passwords. LCSAS does not enforce password distinctness. This isolation
is not privacy between people: one owner per archive holds every
repository's password, and people who need privacy from each other keep
separate archives.

**Mechanism:**

1. Each repository is `rustic init`-ed with its own password before LCSAS
   sees it; rustic stores a password-wrapped master key in
   `<mirror>/keys/<id>`. LCSAS only ever invokes rustic with
   `--password-file <repo's file>`. (`SubprocessRusticRunner._run` in
   `src/lcsas/rustic/wrapper.py`)
2. Packs on the mirror are already rustic-encrypted when LCSAS picks
   them up and are content-addressed by ciphertext hash; LCSAS never
   decrypts. (`scan_mirror_packs` in `src/lcsas/packs/scanner.py`)
3. The catalog scopes every `packs` and `snapshots` row by `repo_id`,
   so `get_unarchived_packs(repo_id=...)` and restore planning are
   scoped to one repository. (`get_unarchived_packs` in `src/lcsas/db/queries.py`,
   `tests/unit/test_multi_tenant.py`)
4. Bin-packing mixes repositories on a disc but keeps each repository's
   metadata under its own `<staging>/metadata/<repo_id>/`.
   (`BurnOrchestrator` in `src/lcsas/burn/orchestrator.py`)

**Guarantees:**

- **Cryptographic isolation:** repository A's password cannot decrypt
  repository B's packs or read its snapshots even on a shared disc. Rustic
  is the sole mechanism; LCSAS adds no additional layer.
- **Catalog isolation:** every pack-level query carries `repo_id`;
  `repo remove --force` does not perturb others
  (`test_deleting_repo_does_not_affect_others`).
- **Restore isolation:** `RestorePlanner` scopes pick lists by snapshot,
  which is scoped by `repo_id`.

**Non-guarantees (known gaps):**

- **Existence side-channel:** every disc names every registered repository
  in `PASSWORD_INFO.txt` / `CONFIG_SUMMARY.txt` and carries a
  `metadata/<repo_id>/` subtree even with zero packs from that repository.
  One disc reveals every repository's name.
  (`HolographicInjector.write_key_info` and
  `HolographicInjector.write_config_summary` in
  `src/lcsas/staging/metadata.py`)
- **Pack-size side-channel:** the disc catalog (`catalog.db`) on every
  disc enumerates `(pack_id, repo_id, size_bytes)` for **all** packs
  across all repositories. Any single disc leaks every other
  repository's backup size and count.
- **Shared-password risk:** nothing prevents two repositories from sharing a
  `password_file`; if they do, "isolation" is just naming.
- **Key file on disc:** the password-encrypted rustic key file is on
  every disc. Weak passwords are crackable offline from any disc — a
  rustic property amplified by copying repository metadata onto every
  disc.
- **`encryption_key_id` is advisory:** captured from the first
  `keys/` filename at `repo add` time; key rotation does not update
  the catalog.

**Variant axes that apply:**

- Multi-repository: the whole concern.
- Storage stage: must hold in the mirror, in the staging area and on
  the discs.
- All others: N/A.

**Test coverage:**

- Existing: `tests/unit/test_multi_tenant.py` (entire file —
  `test_packs_scoped_to_repo`, `test_unarchived_scoped_to_repo`,
  `test_archiving_one_repo_leaves_other_unarchived`,
  `test_pick_list_single_repo_packs`, etc.);
  `tests/unit/test_staging.py::test_write_key_info_with_repos`.
- Gaps:
  - No test pins the "all repositories' metadata go on every disc"
    contract.
  - No test of cross-repository restore refusal (repository A's
    password vs. repository B's metadata).
  - The disc catalog as a side channel is not in the threat model.
  - No test rejects or warns when two repositories share a
    `password_file`.

**Source refs:**

- Catalog scoping: `Repository` in `src/lcsas/db/models.py`,
  `get_unarchived_packs` in `src/lcsas/db/queries.py`
- Bin-pack groups by repository at stage time:
  `BurnOrchestrator` in `src/lcsas/burn/orchestrator.py`
- Per-disc, per-repository metadata trees:
  `HolographicInjector.inject_metadata` in `src/lcsas/staging/metadata.py`
- Survivability disclosure surface:
  `HolographicInjector.write_key_info` /
  `HolographicInjector.write_config_summary` in
  `src/lcsas/staging/metadata.py`
- Rustic password isolation:
  `SubprocessRusticRunner._run` in `src/lcsas/rustic/wrapper.py`
- Architecture overview: [`docs/architecture.md`](../architecture.md)
